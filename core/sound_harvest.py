"""Sound-pivot harvest — the "reused most" data path.

The song aggregate in core/songs.py can only count the sounds it happens to see in
harvested posts, so reuse counts there top out at "how many of this sound's videos
we scraped" (usually 1). This module fixes that by pivoting on the PLATFORM's own
sound page:

  * TikTok  — ``/api/music/detail`` returns ``stats.videoCount`` (videos using the
              sound across all of TikTok) + the videos using it.
  * Instagram — the audio page (``clips/music/``) returns the reels using an audio
              and a ``formatted_clips_media_count`` ("1.2M") we parse to a magnitude.

For each candidate sound we:
  1. fetch its authoritative reuse count + metadata  -> upsert a ``Sound`` row, and
  2. upsert the sample of videos/reels using it as Posts (source=``sound_pivot``),
     so they enrich the corpus AND make the derived post-count meaningful too.

Candidates come from two places, in priority order:
  * platform trending-sound discovery (TikTok FYP sounds / IG ``music_top_trends``)
    — these are reused-a-lot by definition, the best seeds, and
  * the sounds already in our corpus (so everything we show gets a real count),
    refreshed only when stale (see storage.stale_or_missing_sound_keys).

Best-effort and bounded: a single sound failing (geo-block, deleted, back-off) is
logged and skipped; one platform failing never sinks the other.
"""

from __future__ import annotations

import hashlib
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Optional

from core.songs import SONG_PLATFORMS, song_key_for
from core.storage import (
    Post,
    Sound,
    get_session,
    init_db,
    stale_or_missing_sound_keys,
    upsert_post,
    upsert_sound,
)

# Pacing between sound pivots (each is a signed request / private call — be polite).
_PIVOT_PACING_SEC = (1.5, 3.5)

_ROOT = Path(__file__).parent.parent
_SOUND_AUDIO_DIR = _ROOT / "data" / "media" / "sounds"
_AUDIO_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def sound_audio_dir(platform: str, key: str) -> Path:
    """Deterministic per-sound media dir (hashed key — keys can be unicode/`name:`)."""
    h = hashlib.sha1(f"{platform}:{key}".encode("utf-8")).hexdigest()[:20]
    return _SOUND_AUDIO_DIR / platform / h


def cached_audio_file(platform: str, key: str) -> Optional[Path]:
    """The downloaded audio file for a song, if already on disk."""
    d = sound_audio_dir(platform, key)
    if d.is_dir():
        for f in sorted(d.glob("audio.*")):
            if f.is_file() and f.stat().st_size > 0:
                return f
    return None


def download_sound_audio(
    play_url: Optional[str], platform: str, key: str, *, timeout: float = 20.0
) -> Optional[Path]:
    """Download a sound's audio to its cache dir (idempotent). Returns the path.

    Best-effort: a missing url / network / CDN-expiry miss returns None rather than
    raising (a failed audio fetch must never sink a pivot). The extension is taken
    from the URL (TikTok playUrl is .mp3; IG progressive_download_url is .mp4/.m4a),
    defaulting to .mp3.
    """
    existing = cached_audio_file(platform, key)
    if existing is not None:
        return existing
    if not play_url:
        return None
    ext = ".mp3"
    for cand in (".mp3", ".m4a", ".mp4", ".aac", ".ogg", ".wav"):
        if cand in play_url.lower().split("?")[0]:
            ext = cand
            break
    dest_dir = sound_audio_dir(platform, key)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"audio{ext}"
    try:
        req = urllib.request.Request(play_url, headers={"User-Agent": _AUDIO_UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        if not data:
            return None
        dest.write_bytes(data)
        return dest
    except Exception as exc:  # noqa: BLE001 — best-effort
        print(f"[sound] audio download failed for {platform}:{key} (non-fatal): "
              f"{type(exc).__name__}: {str(exc)[:120]}")
        return None


def _corpus_sound_ids(session, platform: str, *, limit: int) -> list[str]:
    """Distinct platform sound ids in our corpus, most-observed first.

    These are the sounds already on screen; pivoting them attaches a real reuse
    count to things the user can actually see. Name-only sounds (no stable id)
    can't be pivoted, so they're excluded here.
    """
    rows = (
        session.query(Post.sound_id)
        .filter(Post.platform == platform, Post.sound_id.isnot(None))
        .all()
    )
    counts = Counter(r[0] for r in rows if r[0])
    return [sid for sid, _ in counts.most_common(limit)]


def _discover_trending_sound_ids(adapter, platform: str, *, limit: int) -> list[str]:
    """Platform trending-sound ids (the reused-a-lot seeds), best-effort."""
    try:
        if platform == "instagram":
            return adapter.fetch_trending_sound_ids(limit=limit)
        if platform == "tiktok":
            # TikTok's FYP videos are surfaced by the discovery harvest; their music
            # ids are trending sounds. Reuse fetch_viral_posts and pull the ids.
            recs = adapter.fetch_viral_posts(geo_tier="KZ", period_days=7)
            ids: list[str] = []
            seen: set[str] = set()
            for r in recs:
                if r.sound_id and r.sound_id not in seen:
                    seen.add(r.sound_id)
                    ids.append(r.sound_id)
                if len(ids) >= limit:
                    break
            return ids
    except Exception as exc:  # discovery is a bonus, never fatal
        print(f"[sound] {platform} trending discovery failed (non-fatal): "
              f"{type(exc).__name__}: {str(exc)[:140]}")
    return []


def _pivot_one(session, adapter, platform: str, seed_id: str, *, videos_per_sound: int) -> dict:
    """Pivot a single sound: upsert its videos + the authoritative Sound row.

    Returns ``{key, video_count, posts_new, posts_seen}`` or ``{error}``.
    """
    sound_rec, posts = adapter.fetch_sound(seed_id, videos_limit=videos_per_sound)

    posts_new = 0
    for rec in posts:
        if not rec.raw or not rec.account_handle:
            continue
        is_new = session.get(Post, (rec.platform, rec.platform_post_id)) is None
        upsert_post(session, rec, source="sound_pivot")
        if is_new:
            posts_new += 1

    # Key the Sound row by the SAME key the posts group under, so it joins the
    # song aggregate cleanly. Posts win (they define the grouping); fall back to
    # the sound record's own id, then the seed id.
    post_keys = [
        song_key_for(p.sound_id, p.sound_name) for p in posts if (p.sound_id or p.sound_name)
    ]
    post_keys = [k for k in post_keys if k]
    key = (
        Counter(post_keys).most_common(1)[0][0]
        if post_keys
        else song_key_for(sound_rec.sound_id, sound_rec.title) or str(seed_id)
    )

    upsert_sound(session, key, sound_rec)
    session.commit()

    # Pre-download the audio while the play_url is fresh (CDN urls expire), so the
    # download button just serves a cached file later.
    audio = download_sound_audio(sound_rec.play_url, platform, key)

    return {
        "key": key,
        "video_count": sound_rec.video_count,
        "title": sound_rec.title,
        "posts_new": posts_new,
        "posts_seen": len(posts),
        "audio": bool(audio),
    }


def harvest_sounds(
    *,
    platforms: Optional[list[str]] = None,
    sound_ids: Optional[dict[str, list[str]]] = None,
    max_sounds_per_platform: int = 25,
    videos_per_sound: int = 30,
    include_trending: bool = True,
    include_corpus: bool = True,
    refresh_stale_only: bool = True,
) -> dict:
    """Pivot candidate sounds into authoritative Sound rows + their videos.

    Parameters
    ----------
    platforms:
        Subset of {tiktok, instagram}. Defaults to both.
    sound_ids:
        Explicit ``{platform: [sound_id, ...]}`` to pivot (highest priority).
    max_sounds_per_platform:
        Cap per platform (each pivot is a network round-trip — keep it bounded).
    videos_per_sound:
        How many videos/reels to pull per sound (they're upserted as posts too).
    include_trending / include_corpus:
        Where to source candidates when ``sound_ids`` doesn't fill the budget.
    refresh_stale_only:
        Skip sounds whose Sound row was refreshed recently (see storage helper).
    """
    init_db()
    platforms = platforms or sorted(SONG_PLATFORMS)
    sound_ids = sound_ids or {}
    session = get_session()
    summary: dict[str, dict] = {}
    try:
        for platform in platforms:
            if platform not in SONG_PLATFORMS:
                continue
            result = {"pivoted": 0, "posts_new": 0, "with_count": 0, "error": None, "sounds": []}
            try:
                adapter = _make_sound_adapter(platform)

                # Assemble candidate seed ids: explicit -> trending -> corpus.
                candidates: list[str] = list(sound_ids.get(platform, []))
                if include_trending and len(candidates) < max_sounds_per_platform:
                    for sid in _discover_trending_sound_ids(
                        adapter, platform, limit=max_sounds_per_platform
                    ):
                        if sid not in candidates:
                            candidates.append(sid)
                if include_corpus and len(candidates) < max_sounds_per_platform:
                    for sid in _corpus_sound_ids(
                        session, platform, limit=max_sounds_per_platform
                    ):
                        if sid not in candidates:
                            candidates.append(sid)
                candidates = candidates[:max_sounds_per_platform]

                # Skip ones already freshly pivoted (unless explicitly requested).
                if refresh_stale_only and not sound_ids.get(platform):
                    keep = stale_or_missing_sound_keys(
                        session, [(platform, c) for c in candidates]
                    )
                    keep_ids = {k for (_p, k) in keep}
                    candidates = [c for c in candidates if c in keep_ids]

                print(f"[sound] {platform}: pivoting {len(candidates)} candidate sounds")
                for seed_id in candidates:
                    try:
                        info = _pivot_one(
                            session, adapter, platform, seed_id,
                            videos_per_sound=videos_per_sound,
                        )
                    except Exception as exc:
                        session.rollback()
                        print(f"[sound] {platform} pivot {seed_id} failed (non-fatal): "
                              f"{type(exc).__name__}: {str(exc)[:140]}")
                        continue
                    result["pivoted"] += 1
                    result["posts_new"] += info["posts_new"]
                    if info["video_count"] is not None:
                        result["with_count"] += 1
                    result["sounds"].append(info)
                    print(f"[sound] {platform} {info.get('title')!r}: "
                          f"video_count={info['video_count']} "
                          f"(+{info['posts_new']} new posts)")
                    time.sleep(_pacing())
            except Exception as exc:
                session.rollback()
                result["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
                print(f"[sound] {platform} harvest failed (non-fatal): {result['error']}")
            summary[platform] = result
    finally:
        session.close()
    print(f"[sound] harvest summary: "
          f"{ {p: {'pivoted': r['pivoted'], 'posts_new': r['posts_new']} for p, r in summary.items()} }")
    return summary


def _pacing() -> float:
    import random
    return random.uniform(*_PIVOT_PACING_SEC)


def _make_sound_adapter(platform: str):
    """Construct the adapter for a sound pivot (reuses ingest's IG burner wiring)."""
    if platform == "tiktok":
        from adapters.tiktok import TikTokAdapter
        return TikTokAdapter()
    if platform == "instagram":
        from core.ingest import _make_instagram_adapter
        # hydrate_views off: per-reel view calls would multiply ban surface.
        return _make_instagram_adapter(hydrate_views=False)
    raise ValueError(f"unsupported sound platform {platform!r}")


if __name__ == "__main__":  # pragma: no cover — manual smoke
    import argparse

    parser = argparse.ArgumentParser(description="Pivot trending/corpus sounds for reuse counts")
    parser.add_argument("--platforms", nargs="*", default=None)
    parser.add_argument("--max", type=int, default=10)
    parser.add_argument("--videos", type=int, default=20)
    args = parser.parse_args()
    out = harvest_sounds(
        platforms=args.platforms,
        max_sounds_per_platform=args.max,
        videos_per_sound=args.videos,
    )
    print(out)
