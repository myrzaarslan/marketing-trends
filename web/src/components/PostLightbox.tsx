import { useEffect, useRef, useState } from 'react';
import type { Collection, ContentBundle, DigestCard } from '../types';
import { enrichPost, fetchPost } from '../api';
import { StatsChart } from './StatsChart';
import { profileUrl } from '../platformLinks';
import { SaveMenu } from './SaveMenu';
import { NoteEditor } from './NoteEditor';
import './PostLightbox.css';

interface Props {
  card: DigestCard;
  rank: number;
  onClose: () => void;
  collections: Collection[];
  onTogglePin: (card: DigestCard) => void;
  onToggleHide: (card: DigestCard) => void;
  onToggleCollection: (card: DigestCard, collectionId: number, makeMember: boolean) => void;
  onCreateCollection: (title: string) => Promise<Collection | null>;
  onSaveNote: (card: DigestCard, body: string) => void;
  /** Fired when on-demand enrichment produced media, so the list can show its thumbnail. */
  onEnriched?: (card: DigestCard, thumbnail: string | null) => void;
}

const PLATFORM_LABELS: Record<string, string> = {
  tiktok: 'TikTok',
  instagram: 'Instagram',
  x: 'X',
  threads: 'Threads',
};

// Signals that each platform actually exposes (SIGNALS.md)
const PLATFORM_SIGNALS: Record<string, Set<string>> = {
  tiktok:    new Set(['view_count', 'like_count', 'comment_count', 'share_count', 'save_count']),
  instagram: new Set(['view_count', 'like_count', 'comment_count']),
  x:         new Set(['like_count', 'comment_count', 'share_count']),
  threads:   new Set(['like_count', 'comment_count', 'share_count']),
};

function hasSignal(platform: string, signal: string): boolean {
  return PLATFORM_SIGNALS[platform]?.has(signal) ?? true;
}

function fmt(n: number | null | undefined): string {
  if (n == null) return '–';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function fmtScore(score: number | null | undefined): string {
  if (score == null) return '–';
  if (score > 1000) return fmt(Math.round(score));
  if (score > 1) return score.toFixed(1);
  return score.toFixed(4);
}

function timeAgo(iso: string | null | undefined): string {
  if (!iso) return '–';
  const ms = Date.now() - new Date(iso).getTime();
  const h = Math.floor(ms / 3600000);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d ago`;
  return `${Math.floor(d / 30)}mo ago`;
}


// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function Carousel({ items, platform }: { items: ContentBundle['media_items']; platform: string }) {
  const [idx, setIdx] = useState(0);
  const videoRef = useRef<HTMLVideoElement>(null);
  const images = items.filter((i) => i.kind === 'image');
  const videos = items.filter((i) => i.kind === 'video');
  // Prefer videos first, then images
  const mediaItems = [...videos, ...images];

  if (mediaItems.length === 0) return null;

  const current = mediaItems[idx];
  const total = mediaItems.length;

  const prev = () => setIdx((i) => (i - 1 + total) % total);
  const next = () => setIdx((i) => (i + 1) % total);

  const handleKey = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowLeft') prev();
    if (e.key === 'ArrowRight') next();
  };

  return (
    <div className="lb-media" onKeyDown={handleKey} tabIndex={0}>
      {current.kind === 'video' ? (
        <video
          ref={videoRef}
          key={current.url}
          src={current.url}
          controls
          playsInline
          className="lb-video"
          aria-label={`${PLATFORM_LABELS[platform] ?? platform} video`}
        />
      ) : (
        <img
          key={current.url}
          src={current.url}
          alt={`Slide ${idx + 1} of ${total}`}
          className="lb-image"
          loading="lazy"
        />
      )}

      {total > 1 && (
        <div className="lb-carousel-controls">
          <button
            className="lb-arrow lb-arrow-prev"
            onClick={prev}
            aria-label="Previous"
            disabled={total <= 1}
          >
            ‹
          </button>
          <span className="lb-slide-count">{idx + 1} / {total}</span>
          <button
            className="lb-arrow lb-arrow-next"
            onClick={next}
            aria-label="Next"
            disabled={total <= 1}
          >
            ›
          </button>
        </div>
      )}
    </div>
  );
}

function SpoilerText({ text }: { text: string }) {
  const [revealed, setReveal] = useState(false);
  return (
    <div
      className={`lb-spoiler${revealed ? ' lb-spoiler--revealed' : ''}`}
      onClick={() => setReveal(true)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => e.key === 'Enter' && setReveal(true)}
      aria-label={revealed ? undefined : 'Click to reveal spoiler content'}
    >
      <p className="lb-spoiler-text">{text}</p>
      {!revealed && (
        <div className="lb-spoiler-overlay">
          <span className="lb-spoiler-hint">⚠ Sensitive content — click to reveal</span>
        </div>
      )}
    </div>
  );
}

function MusicChip({ bundle }: { bundle: ContentBundle }) {
  const [playing, setPlaying] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  if (!bundle.sound_name && !bundle.sound_id) return null;

  const audioItem = bundle.media_items.find((i) => i.kind === 'audio');

  const togglePlay = () => {
    if (!audioRef.current) return;
    if (playing) {
      audioRef.current.pause();
      setPlaying(false);
    } else {
      audioRef.current.play();
      setPlaying(true);
    }
  };

  return (
    <div className="lb-music-chip">
      {audioItem && (
        <audio ref={audioRef} src={audioItem.url} onEnded={() => setPlaying(false)} />
      )}
      <span className="lb-music-icon">♫</span>
      <div className="lb-music-info">
        {bundle.sound_name && <span className="lb-music-name">{bundle.sound_name}</span>}
        {bundle.sound_author && <span className="lb-music-author">{bundle.sound_author}</span>}
      </div>
      {audioItem && (
        <button
          className={`lb-music-play${playing ? ' lb-music-play--playing' : ''}`}
          onClick={togglePlay}
          aria-label={playing ? 'Pause audio' : 'Play audio'}
        >
          {playing ? '⏸' : '▶'}
        </button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// TextHero for X/Threads posts
// ---------------------------------------------------------------------------

function TextHero({ caption }: { caption: string }) {
  return (
    <div className="lb-text-hero">
      <p className="lb-text-hero-body">{caption}</p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main lightbox
// ---------------------------------------------------------------------------

export function PostLightbox({
  card,
  rank,
  onClose,
  collections,
  onTogglePin,
  onToggleHide,
  onToggleCollection,
  onCreateCollection,
  onSaveNote,
  onEnriched,
}: Props) {
  const [bundle, setBundle] = useState<ContentBundle | null>(null);
  const [loading, setLoading] = useState(true);
  const [enriching, setEnriching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saveOpen, setSaveOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setEnriching(false);
    setError(null);
    fetchPost(card.platform, card.platform_post_id)
      .then((b) => {
        if (cancelled) return;
        setBundle(b);
        setLoading(false);
        // Media not downloaded yet → priority-enrich this post on demand.
        if (!b.enriched) {
          setEnriching(true);
          enrichPost(card.platform, card.platform_post_id)
            .then((nb) => {
              if (cancelled) return;
              setBundle(nb);
              if (nb.enriched) onEnriched?.(card, nb.thumbnail ?? null);
            })
            .catch(() => { /* keep the un-enriched bundle (thumbnail/text still show) */ })
            .finally(() => { if (!cancelled) setEnriching(false); });
        }
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
        setLoading(false);
      });
    return () => { cancelled = true; };
  }, [card.platform, card.platform_post_id]);

  // Close on Escape
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [onClose]);

  // Prevent body scroll while open
  useEffect(() => {
    document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = ''; };
  }, []);

  const platform = card.platform;
  const platformLabel = PLATFORM_LABELS[platform] ?? platform;
  const rankStr = `#${String(rank).padStart(3, '0')}`;
  const b = bundle;

  // Determine which media to show
  const mediaItems = b?.media_items ?? [];
  const hasVideo = mediaItems.some((i) => i.kind === 'video');
  const hasImages = mediaItems.some((i) => i.kind === 'image');
  const isTextPost = !hasVideo && !hasImages && (platform === 'x' || platform === 'threads');

  const caption = b?.caption ?? card.caption;
  const hashtags = b?.hashtags ?? card.hashtags ?? [];

  // Only treat a stat as present if the platform exposes it AND we have a value.
  // Never render a fake 0 (SIGNALS.md).
  const hasAnyStat = b != null && (
    (hasSignal(platform, 'view_count') && b.view_count != null) ||
    (hasSignal(platform, 'like_count') && b.like_count != null) ||
    (hasSignal(platform, 'comment_count') && b.comment_count != null) ||
    (hasSignal(platform, 'share_count') && b.share_count != null) ||
    (hasSignal(platform, 'save_count') && b.save_count != null)
  );

  return (
    <div
      className="lb-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label="Post viewer"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="lb-panel">
        {/* Header bar */}
        <header className="lb-header">
          <div className="lb-header-left">
            <span className="lb-rank">{rankStr}</span>
            <span className="lb-platform-label lb-platform-label--{platform}"
              data-platform={platform}
            >
              {platformLabel}
            </span>
            {(b?.geo_tier ?? card.geo_tier) && (
              <span className="lb-geo">{b?.geo_tier ?? card.geo_tier}</span>
            )}
            <span className="lb-handle">
              {(() => {
                const handle = b?.account_handle ?? card.account_handle;
                const href = profileUrl(platform, handle);
                return href ? (
                  <a
                    className="lb-handle-link"
                    href={href}
                    target="_blank"
                    rel="noopener noreferrer"
                    title={`Open @${handle} on ${platformLabel}`}
                  >
                    @{handle}
                  </a>
                ) : (
                  <>@{handle}</>
                );
              })()}
              {b?.author_display_name && b.author_display_name !== b.account_handle && (
                <span className="lb-display-name">{b.author_display_name}</span>
              )}
            </span>
          </div>
          <div className="lb-header-right">
            {b && b.score != null && (
              <span className="lb-score-chip" title={`Sort: ${b.sort_used}`}>
                <span className="lb-score-label">{b.sort_used?.replace(/_/g, ' ')}</span>
                <span className="lb-score-val">{fmtScore(b.score)}</span>
              </span>
            )}

            <div className="lb-actions">
              <button
                type="button"
                className={`lb-action${card.pinned ? ' lb-action--on' : ''}`}
                onClick={() => onTogglePin(card)}
                title={card.pinned ? 'Pinned — stays across refresh' : 'Pin (keep across refresh)'}
                aria-pressed={card.pinned}
              >
                📌
              </button>
              <div className="lb-action-wrap">
                <button
                  type="button"
                  className={`lb-action${(card.collection_ids?.length ?? 0) > 0 ? ' lb-action--on' : ''}`}
                  onClick={() => setSaveOpen((v) => !v)}
                  title="Save to collection"
                  aria-pressed={(card.collection_ids?.length ?? 0) > 0}
                >
                  🔖
                </button>
                {saveOpen && (
                  <SaveMenu
                    collections={collections}
                    memberIds={card.collection_ids ?? []}
                    onToggle={(cid, makeMember) => onToggleCollection(card, cid, makeMember)}
                    onCreate={onCreateCollection}
                    onClose={() => setSaveOpen(false)}
                  />
                )}
              </div>
              <button
                type="button"
                className={`lb-action${card.hidden ? ' lb-action--on' : ''}`}
                onClick={() => onToggleHide(card)}
                title={card.hidden ? 'Hidden — won’t appear in digest' : 'Hide (don’t show me this)'}
                aria-pressed={card.hidden}
              >
                🚫
              </button>
            </div>

            <button className="lb-close" onClick={onClose} aria-label="Close viewer">✕</button>
          </div>
        </header>

        {/* Body */}
        <div className="lb-body">
          {loading && (
            <div className="lb-loading">
              <div className="lb-loading-inner">Loading specimen…</div>
            </div>
          )}

          {error && !loading && (
            <div className="lb-error">
              <p>Failed to load Content Bundle.</p>
              <p className="lb-error-detail">{error}</p>
              {/* Graceful degradation: show what we have from the card */}
              <GracefulCard card={card} rank={rank} />
            </div>
          )}

          {!loading && !error && b && (
            <>
              {/* Left: Media column */}
              <div className="lb-left">
                {isTextPost && caption ? (
                  <TextHero caption={caption} />
                ) : mediaItems.length > 0 ? (
                  b.has_spoiler ? (
                    <div className="lb-media-spoiler-wrap">
                      <Carousel items={mediaItems} platform={platform} />
                      <div className="lb-media-spoiler-overlay">
                        <span>⚠ Sensitive — scroll past to view</span>
                      </div>
                    </div>
                  ) : (
                    <Carousel items={mediaItems} platform={platform} />
                  )
                ) : (
                  // No media yet: show thumbnail + (priority download) status overlay.
                  <div className="lb-media lb-media--placeholder">
                    {card.thumbnail ? (
                      <img src={card.thumbnail} alt="thumbnail" className="lb-image lb-image--thumb" />
                    ) : (
                      <div className="lb-media-empty">
                        <span className="lb-media-empty-label">{card.media_type.toUpperCase()}</span>
                        <span className="lb-media-empty-sub">
                          {enriching ? 'Downloading media…' : 'No media downloaded'}
                        </span>
                      </div>
                    )}
                    {enriching && (
                      <div className="lb-enriching-overlay">
                        <span className="lb-enriching-spinner" aria-hidden />
                        <span className="lb-enriching-label">Downloading media…</span>
                      </div>
                    )}
                  </div>
                )}

                {/* Music chip */}
                <MusicChip bundle={b} />
              </div>

              {/* Right: Info column */}
              <div className="lb-right">
                {/* Caption / spoiler */}
                {b.has_spoiler && b.spoiler_text ? (
                  <SpoilerText text={b.spoiler_text} />
                ) : caption ? (
                  <div className="lb-section">
                    <h3 className="lb-section-label">Caption</h3>
                    <p className="lb-caption">{caption}</p>
                  </div>
                ) : null}

                {/* Hashtags */}
                {hashtags.length > 0 && (
                  <div className="lb-hashtags">
                    {hashtags.map((h) => (
                      <span key={h} className="lb-hashtag">#{h}</span>
                    ))}
                  </div>
                )}

                {/* Note — global per post */}
                <div className="lb-section">
                  <h3 className="lb-section-label">Note</h3>
                  <NoteEditor value={card.note} onSave={(body) => onSaveNote(card, body)} />
                </div>

                {/* Stats — only show signals the platform actually exposes,
                    and only render the section if at least one value exists
                    (never a fake 0 — SIGNALS.md). */}
                {hasAnyStat ? (
                  <div className="lb-section">
                    <h3 className="lb-section-label">Engagement</h3>
                    <dl className="lb-stats">
                      {hasSignal(platform, 'view_count') && (
                        <StatRow label="Views" value={b.view_count} />
                      )}
                      {hasSignal(platform, 'like_count') && (
                        <StatRow label="Likes" value={b.like_count} />
                      )}
                      {hasSignal(platform, 'comment_count') && (
                        <StatRow label="Comments" value={b.comment_count} />
                      )}
                      {hasSignal(platform, 'share_count') && (
                        <StatRow label="Shares / Reposts" value={b.share_count} />
                      )}
                      {hasSignal(platform, 'save_count') && (
                        <StatRow label="Saves" value={b.save_count} />
                      )}
                    </dl>
                  </div>
                ) : (
                  <div className="lb-section">
                    <h3 className="lb-section-label">Engagement</h3>
                    <p className="lb-stats-empty">No counts captured for this specimen yet.</p>
                  </div>
                )}

                {/* Engagement trend over time (snapshot series + velocity) */}
                <div className="lb-section">
                  <StatsChart platform={platform} platformPostId={card.platform_post_id} />
                </div>

                {/* Provenance */}
                <div className="lb-section lb-provenance">
                  <h3 className="lb-section-label">Provenance</h3>
                  <dl className="lb-prov-grid">
                    <dt>Platform</dt>
                    <dd>{platformLabel}</dd>
                    {b.geo_tier && <><dt>Geo</dt><dd>{b.geo_tier}</dd></>}
                    <dt>Posted</dt>
                    <dd title={b.posted_at ?? undefined}>{timeAgo(b.posted_at)}</dd>
                    <dt>First seen</dt>
                    <dd title={b.first_seen_at}>{timeAgo(b.first_seen_at)}</dd>
                    {b.author_follower_count != null && (
                      <><dt>Followers</dt><dd>{fmt(b.author_follower_count)}</dd></>
                    )}
                    {!b.enriched && (
                      <><dt>Bundle</dt><dd className="lb-prov-degraded">Not enriched</dd></>
                    )}
                  </dl>
                  <a
                    href={b.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="lb-open-original"
                  >
                    Open original ↗
                  </a>
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function StatRow({ label, value }: { label: string; value: number | null | undefined }) {
  if (value == null) return null;
  return (
    <>
      <dt>{label}</dt>
      <dd className="lb-stat-val">{fmt(value)}</dd>
    </>
  );
}

// Graceful degradation — shown when API call fails or post not enriched
function GracefulCard({ card }: { card: DigestCard; rank: number }) {
  return (
    <div className="lb-graceful">
      <div className="lb-graceful-thumb">
        {card.thumbnail ? (
          <img src={card.thumbnail} alt="thumbnail" />
        ) : (
          <div className="lb-graceful-thumb-placeholder">
            {(PLATFORM_LABELS[card.platform] ?? card.platform)[0]}
          </div>
        )}
      </div>
      <div className="lb-graceful-info">
        {(() => {
          const href = profileUrl(card.platform, card.account_handle);
          return href ? (
            <a
              className="lb-graceful-handle lb-handle-link"
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              title={`Open @${card.account_handle} on ${PLATFORM_LABELS[card.platform] ?? card.platform}`}
            >
              @{card.account_handle}
            </a>
          ) : (
            <span className="lb-graceful-handle">@{card.account_handle}</span>
          );
        })()}
        {card.caption && <p className="lb-graceful-caption">{card.caption.slice(0, 200)}</p>}
        <div className="lb-graceful-stats">
          {card.like_count != null && <span>♥ {fmt(card.like_count)}</span>}
          {card.comment_count != null && <span>💬 {fmt(card.comment_count)}</span>}
        </div>
        <a href={card.url} target="_blank" rel="noopener noreferrer" className="lb-open-original">
          Open original ↗
        </a>
      </div>
    </div>
  );
}
