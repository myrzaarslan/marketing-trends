import { useCallback, useEffect, useRef, useState } from 'react';
import {
  addToCollection,
  deleteNote,
  enrichPost,
  fetchRefreshStatus,
  fetchSongDetail,
  fetchSongs,
  putFlags,
  putNote,
  putSongFlags,
  removeFromCollection,
  songAudioUrl,
  triggerSongHardRefresh,
} from '../api';
import type {
  Collection,
  DigestCard as DigestCardType,
  RefreshSource,
  Song,
  SongFilters,
  SongSortKey,
} from '../types';
import { DigestCard } from './DigestCard';
import { SongCard } from './SongCard';
import { PostLightbox } from './PostLightbox';
import './SongsView.css';

interface Props {
  collections: Collection[];
  onCreateCollection: (title: string) => Promise<Collection | null>;
  reloadCollections: () => void;
}

const DEFAULT_FILTERS: SongFilters = {
  platform: '',
  geo: '',
  period: 30,
  sort: 'reuse_count',
  limit: 60,
  unseen_only: false,
};

const SORT_LABELS: Record<SongSortKey, string> = {
  reuse_count: 'Reused most (videos using it)',
  post_count: 'Adoption (post count)',
  total_views: 'Total reach (views)',
  total_engagement: 'Total engagement',
  avg_engagement_rate: 'Avg engagement rate',
  rising: 'Rising (recent adoption)',
};

const ALL_SORTS: SongSortKey[] = [
  'reuse_count',
  'post_count',
  'total_views',
  'total_engagement',
  'avg_engagement_rate',
  'rising',
];

const SONG_PLATFORMS = ['tiktok', 'instagram'] as const;
const GEOS = ['KZ', 'CIS', 'World'];
const PERIODS = [7, 14, 30, 60, 90];
const LIMITS = [30, 60, 100, 150];

const songKeyOf = (s: { platform: string; key: string }) => `${s.platform}:${s.key}`;
const postKeyOf = (c: { platform: string; platform_post_id: string }) =>
  `${c.platform}:${c.platform_post_id}`;

export function SongsView({ collections, onCreateCollection, reloadCollections }: Props) {
  const [filters, setFilters] = useState<SongFilters>(DEFAULT_FILTERS);
  const [songs, setSongs] = useState<Song[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshStatus, setRefreshStatus] = useState<string | null>(null);

  // Song detail (the posts that use one song)
  const [activeSong, setActiveSong] = useState<Song | null>(null);
  const [detailCards, setDetailCards] = useState<DigestCardType[]>([]);

  // Selective refresh of songs
  const [selectionMode, setSelectionMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [menuOpen, setMenuOpen] = useState(false);

  // Lightbox over a detail post
  const [openCard, setOpenCard] = useState<DigestCardType | null>(null);
  const [openRank, setOpenRank] = useState(1);

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  // -- loaders ---------------------------------------------------------------

  const loadSongs = useCallback(async (f: SongFilters) => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchSongs(f);
      setSongs(data.songs);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadSongs(filters);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, []);

  const handleFiltersChange = (patch: Partial<SongFilters>) => {
    const next = { ...filters, ...patch };
    setFilters(next);
    if (!activeSong) loadSongs(next);
  };

  // -- song detail -----------------------------------------------------------

  const openSong = useCallback(async (song: Song) => {
    setLoading(true);
    setError(null);
    try {
      const detail = await fetchSongDetail(song, filters);
      setActiveSong(detail.song);
      setDetailCards(detail.cards);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [filters]);

  const backToList = () => {
    setActiveSong(null);
    setDetailCards([]);
  };

  // -- song flag actions -----------------------------------------------------

  const onTogglePinSong = async (song: Song) => {
    const pinned = !song.pinned;
    setSongs((ss) => ss.map((s) => (songKeyOf(s) === songKeyOf(song) ? { ...s, pinned } : s)));
    setActiveSong((s) => (s && songKeyOf(s) === songKeyOf(song) ? { ...s, pinned } : s));
    try { await putSongFlags(song.platform, song.key, { pinned }); }
    catch {
      setSongs((ss) => ss.map((s) => (songKeyOf(s) === songKeyOf(song) ? { ...s, pinned: !pinned } : s)));
    }
  };

  const onToggleHideSong = async (song: Song) => {
    const hidden = !song.hidden;
    try {
      await putSongFlags(song.platform, song.key, { hidden });
      if (hidden) {
        setSongs((ss) => ss.filter((s) => songKeyOf(s) !== songKeyOf(song)));
      } else {
        setSongs((ss) => ss.map((s) => (songKeyOf(s) === songKeyOf(song) ? { ...s, hidden } : s)));
      }
    } catch { /* keep state */ }
  };

  // -- detail post card actions (mirror App) ---------------------------------

  const patchCard = useCallback((k: string, partial: Partial<DigestCardType>) => {
    setDetailCards((cs) => cs.map((c) => (postKeyOf(c) === k ? { ...c, ...partial } : c)));
    setOpenCard((oc) => (oc && postKeyOf(oc) === k ? { ...oc, ...partial } : oc));
  }, []);

  const onEnriched = useCallback((card: DigestCardType, thumbnail: string | null) => {
    patchCard(postKeyOf(card), { thumbnail, has_content_bundle: true, has_content: true });
  }, [patchCard]);

  const onTogglePin = async (card: DigestCardType) => {
    const pinned = !card.pinned;
    patchCard(postKeyOf(card), { pinned });
    try { await putFlags(card.platform, card.platform_post_id, { pinned }); }
    catch { patchCard(postKeyOf(card), { pinned: !pinned }); }
  };

  const onToggleHide = async (card: DigestCardType) => {
    const hidden = !card.hidden;
    patchCard(postKeyOf(card), { hidden });
    try { await putFlags(card.platform, card.platform_post_id, { hidden }); }
    catch { patchCard(postKeyOf(card), { hidden: !hidden }); }
  };

  const onSaveNote = async (card: DigestCardType, body: string) => {
    const prev = card.note ?? null;
    patchCard(postKeyOf(card), { note: body || null });
    try {
      if (body) await putNote(card.platform, card.platform_post_id, body);
      else await deleteNote(card.platform, card.platform_post_id);
    } catch { patchCard(postKeyOf(card), { note: prev }); }
  };

  const onToggleCollection = async (
    card: DigestCardType,
    collectionId: number,
    makeMember: boolean,
  ) => {
    const ids = new Set(card.collection_ids ?? []);
    if (makeMember) ids.add(collectionId); else ids.delete(collectionId);
    patchCard(postKeyOf(card), { collection_ids: [...ids] });
    try {
      if (makeMember) await addToCollection(collectionId, card.platform, card.platform_post_id);
      else await removeFromCollection(collectionId, card.platform, card.platform_post_id);
      reloadCollections();
    } catch { /* best-effort */ }
  };

  // Background pre-download for the detail posts.
  const enrichAttempted = useRef<Set<string>>(new Set());
  useEffect(() => {
    let cancelled = false;
    const queue = detailCards.filter(
      (c) => !c.has_content_bundle && !enrichAttempted.current.has(postKeyOf(c)),
    );
    if (queue.length === 0) return;
    (async () => {
      for (const card of queue) {
        if (cancelled) return;
        const k = postKeyOf(card);
        enrichAttempted.current.add(k);
        try {
          const nb = await enrichPost(card.platform, card.platform_post_id);
          if (cancelled) return;
          if (nb.enriched) {
            patchCard(k, { thumbnail: nb.thumbnail ?? null, has_content_bundle: true, has_content: true });
          }
        } catch {
          enrichAttempted.current.delete(k);
        }
        await new Promise((r) => setTimeout(r, 250));
      }
    })();
    return () => { cancelled = true; };
  }, [detailCards, patchCard]);

  const handleOpenCard = (card: DigestCardType) => {
    const rank = detailCards.findIndex((c) => postKeyOf(c) === postKeyOf(card)) + 1;
    setOpenCard(card);
    setOpenRank(rank || 1);
  };
  const handleCloseCard = useCallback(() => setOpenCard(null), []);

  // -- refresh ---------------------------------------------------------------

  const pollJob = (jobId: string, onDone: () => void) => {
    setRefreshStatus('queued');
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const status = await fetchRefreshStatus(jobId);
        setRefreshStatus(status.status);
        if (status.status === 'done' || status.status === 'error') {
          clearInterval(pollRef.current!);
          pollRef.current = null;
          if (status.status === 'done') onDone();
        }
      } catch {
        clearInterval(pollRef.current!);
        pollRef.current = null;
        setRefreshStatus('error');
      }
    }, 2000);
  };

  const runHardRefresh = async (source: RefreshSource, serveKeys: [string, string][]) => {
    setMenuOpen(false);
    try {
      const { job_id } = await triggerSongHardRefresh({
        source,
        serve_keys: serveKeys,
        platform: filters.platform || null,
        geo: filters.geo || null,
        period: filters.period,
        sort: filters.sort,
        limit: filters.limit,
      });
      pollJob(job_id, () => {
        const next = { ...filters, unseen_only: true };
        setFilters(next);
        setActiveSong(null);
        loadSongs(next);
        setSelected(new Set());
        setSelectionMode(false);
      });
    } catch {
      setRefreshStatus('error');
    }
  };

  const currentServeKeys = (): [string, string][] =>
    songs.map((s) => [s.platform, s.key] as [string, string]);

  const handleSoft = () => { setMenuOpen(false); loadSongs(filters); };
  const handleHardCorpus = () => runHardRefresh('corpus', currentServeKeys());
  const handleHardLive = () => runHardRefresh('live', currentServeKeys());
  const handleRefreshSelected = () => {
    const keys = songs
      .filter((s) => selected.has(songKeyOf(s)))
      .map((s) => [s.platform, s.key] as [string, string]);
    if (keys.length) runHardRefresh('corpus', keys);
  };

  const showAll = () => {
    const next = { ...filters, unseen_only: false };
    setFilters(next);
    loadSongs(next);
  };

  const onToggleSelect = (song: Song) => {
    setSelected((s) => {
      const next = new Set(s);
      const k = songKeyOf(song);
      if (next.has(k)) next.delete(k); else next.add(k);
      return next;
    });
  };

  const refreshRunning = refreshStatus === 'queued' || refreshStatus === 'running';

  const cardProps = {
    collections,
    onTogglePin,
    onToggleHide,
    onToggleCollection,
    onCreateCollection,
    onSaveNote,
  };

  // -- render ----------------------------------------------------------------

  return (
    <div className="songs-view">
      {/* Filter + refresh bar */}
      {!activeSong && (
        <div className="songs-bar">
          <div className="filter-group">
            <label>Platform</label>
            <select
              value={filters.platform}
              onChange={(e) => handleFiltersChange({ platform: e.target.value as SongFilters['platform'] })}
            >
              <option value="">All</option>
              {SONG_PLATFORMS.map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
          </div>
          <div className="filter-group">
            <label>Geo</label>
            <select value={filters.geo} onChange={(e) => handleFiltersChange({ geo: e.target.value })}>
              <option value="">All</option>
              {GEOS.map((g) => <option key={g} value={g}>{g}</option>)}
            </select>
          </div>
          <div className="filter-group">
            <label>Period</label>
            <select value={filters.period} onChange={(e) => handleFiltersChange({ period: Number(e.target.value) })}>
              {PERIODS.map((d) => <option key={d} value={d}>{d}d</option>)}
            </select>
          </div>
          <div className="filter-group sort-group">
            <label>Rank by</label>
            <select value={filters.sort} onChange={(e) => handleFiltersChange({ sort: e.target.value as SongSortKey })}>
              {ALL_SORTS.map((k) => <option key={k} value={k}>{SORT_LABELS[k]}</option>)}
            </select>
          </div>
          <div className="filter-group">
            <label>Limit</label>
            <select value={filters.limit} onChange={(e) => handleFiltersChange({ limit: Number(e.target.value) })}>
              {LIMITS.map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </div>

          <div className="songs-bar-spacer" />

          {refreshStatus && (
            <span className={`refresh-badge refresh-${refreshStatus}`}>
              {refreshStatus === 'queued' && '⟳ Queued'}
              {refreshStatus === 'running' && '⟳ Working…'}
              {refreshStatus === 'done' && '✓ Updated'}
              {refreshStatus === 'error' && '✕ Error'}
            </span>
          )}

          <div className="refresh-menu" ref={menuRef}>
            <button className="btn-refresh" onClick={() => setMenuOpen((v) => !v)} disabled={refreshRunning}>
              {refreshRunning ? 'Refreshing…' : 'Refresh ▾'}
            </button>
            {menuOpen && (
              <div className="refresh-dropdown" role="menu">
                <button type="button" className="refresh-opt" onClick={handleSoft}>
                  <span className="refresh-opt-title">↻ Soft refresh</span>
                  <span className="refresh-opt-sub">Re-rank the current songs in place</span>
                </button>
                <button type="button" className="refresh-opt" onClick={handleHardCorpus}>
                  <span className="refresh-opt-title">✦ Hard refresh — corpus</span>
                  <span className="refresh-opt-sub">{filters.limit} fresh unseen songs from the corpus (fast)</span>
                </button>
                <button type="button" className="refresh-opt" onClick={handleHardLive}>
                  <span className="refresh-opt-title">🛰 Hard refresh — live</span>
                  <span className="refresh-opt-sub">Scrape new posts first, then surface new songs (slow)</span>
                </button>
                <div className="refresh-sep" />
                <button type="button" className="refresh-opt" onClick={() => { setSelectionMode((v) => !v); setMenuOpen(false); }}>
                  <span className="refresh-opt-title">{selectionMode ? '✓ Selecting…' : '☑ Select sounds to refresh'}</span>
                  <span className="refresh-opt-sub">{selectionMode ? 'Pick sounds, then “Refresh selected”' : 'Choose individual sounds to rotate out'}</span>
                </button>
                {selectionMode && (
                  <button
                    type="button"
                    className="refresh-opt refresh-opt--primary"
                    onClick={() => { setMenuOpen(false); handleRefreshSelected(); }}
                    disabled={selected.size === 0}
                  >
                    <span className="refresh-opt-title">→ Refresh {selected.size} selected</span>
                    <span className="refresh-opt-sub">Replace selected with fresh unseen sounds</span>
                  </button>
                )}
              </div>
            )}
          </div>
        </div>
      )}

      <main className="digest-main">
        {error && <div className="error-banner"><strong>Error:</strong> {error}</div>}
        {loading && <div className="loading">Scanning sounds…</div>}

        {/* Song detail */}
        {!loading && !error && activeSong && (
          <>
            <div className="song-detail-head">
              <button className="btn-ghost" onClick={backToList}>← All sounds</button>
              <div className="song-detail-cover">
                {activeSong.thumbnail
                  ? <img src={activeSong.thumbnail} alt={activeSong.sound_name ?? 'cover'} />
                  : <div className="song-detail-cover-ph">♫</div>}
              </div>
              <div className="song-detail-info">
                <h2 className="song-detail-title">{activeSong.sound_name ?? 'Unknown sound'}</h2>
                {activeSong.sound_author && <p className="song-detail-author">{activeSong.sound_author}</p>}
                <div className="song-detail-stats">
                  <span>
                    <b>{activeSong.reuse_count.toLocaleString()}</b>
                    {activeSong.reuse_count_source === 'observed' ? '+' : ''} reuses
                  </span>
                  <span><b>{activeSong.post_count}</b> posts</span>
                  <span><b>{activeSong.distinct_accounts}</b> creators</span>
                  {activeSong.total_views > 0 && <span><b>{activeSong.total_views.toLocaleString()}</b> views</span>}
                  {activeSong.avg_engagement_rate != null && <span><b>{activeSong.avg_engagement_rate.toFixed(3)}</b> avg rate</span>}
                  <span className="song-detail-plat" data-platform={activeSong.platform}>{activeSong.platform}</span>
                </div>
                {activeSong.downloadable && (
                  <a
                    className="song-download-btn"
                    href={songAudioUrl(activeSong.platform, activeSong.key)}
                    download
                  >
                    ⬇ Download audio
                  </a>
                )}
              </div>
            </div>
            {detailCards.length === 0 ? (
              <div className="empty-state">No posts using this sound in the current window.</div>
            ) : (
              <div className="card-grid">
                {detailCards.map((card, i) => (
                  <DigestCard
                    key={postKeyOf(card)}
                    card={card}
                    rank={i + 1}
                    onOpen={handleOpenCard}
                    {...cardProps}
                  />
                ))}
              </div>
            )}
          </>
        )}

        {/* Song list */}
        {!loading && !error && !activeSong && (
          <>
            <div className="result-meta">
              <span className="result-count">{songs.length.toLocaleString()}</span>
              {' sounds'}
              {filters.platform && <span className="result-filter"> · {filters.platform}</span>}
              {filters.geo && <span className="result-filter"> · {filters.geo}</span>}
              <span className="result-filter"> · {filters.period}d window</span>
              <span className="result-filter"> · by {filters.sort.replace(/_/g, ' ')}</span>
              {filters.unseen_only && (
                <span className="working-set-badge">
                  fresh set
                  <button className="working-set-clear" onClick={showAll}>show all</button>
                </span>
              )}
            </div>
            {songs.length === 0 ? (
              <div className="empty-state">
                No sounds found for these filters.
                <br />
                <span className="empty-hint">Try a wider period or a different platform.</span>
              </div>
            ) : (
              <div className="song-grid">
                {songs.map((song, i) => (
                  <SongCard
                    key={songKeyOf(song)}
                    song={song}
                    rank={i + 1}
                    activeSort={filters.sort}
                    onOpen={openSong}
                    onTogglePin={onTogglePinSong}
                    onToggleHide={onToggleHideSong}
                    selectable={selectionMode}
                    selected={selected.has(songKeyOf(song))}
                    onToggleSelect={onToggleSelect}
                  />
                ))}
              </div>
            )}
          </>
        )}
      </main>

      {openCard && (
        <PostLightbox
          card={openCard}
          rank={openRank}
          onClose={handleCloseCard}
          collections={collections}
          onTogglePin={onTogglePin}
          onToggleHide={onToggleHide}
          onToggleCollection={onToggleCollection}
          onCreateCollection={onCreateCollection}
          onSaveNote={onSaveNote}
          onEnriched={onEnriched}
        />
      )}
    </div>
  );
}
