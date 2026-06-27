import type { Song, SongSortKey } from '../types';
import { songAudioUrl } from '../api';
import './SongCard.css';

interface Props {
  song: Song;
  rank: number;
  activeSort: SongSortKey;
  onOpen: (song: Song) => void;
  onTogglePin: (song: Song) => void;
  onToggleHide: (song: Song) => void;
  selectable?: boolean;
  selected?: boolean;
  onToggleSelect?: (song: Song) => void;
}

const PLATFORM_LABELS: Record<string, string> = {
  tiktok: 'TikTok',
  instagram: 'Instagram',
};

const SORT_LABELS: Record<SongSortKey, string> = {
  reuse_count: 'reuses',
  post_count: 'posts',
  total_views: 'total views',
  total_engagement: 'total engagement',
  avg_engagement_rate: 'avg eng. rate',
  rising: 'rising',
};

function fmt(n: number | null | undefined): string {
  if (n == null) return '–';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(Math.round(n));
}

function fmtRate(n: number | null | undefined): string {
  if (n == null) return '–';
  return n.toFixed(3);
}

export function SongCard({
  song,
  rank,
  activeSort,
  onOpen,
  onTogglePin,
  onToggleHide,
  selectable = false,
  selected = false,
  onToggleSelect,
}: Props) {
  const platformLabel = PLATFORM_LABELS[song.platform] ?? song.platform;
  const rankStr = `#${String(rank).padStart(3, '0')}`;
  const stop = (e: React.MouseEvent) => e.stopPropagation();

  const scoreLabel = SORT_LABELS[activeSort];
  const scoreValue =
    activeSort === 'avg_engagement_rate' ? fmtRate(song.score) : fmt(song.score);

  return (
    <article
      className={`song-card${song.pinned ? ' song-card--pinned' : ''}${selected ? ' song-card--selected' : ''}`}
      data-platform={song.platform}
      onClick={() => onOpen(song)}
      onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && onOpen(song)}
      tabIndex={0}
      role="button"
      aria-label={`View sound ${rankStr}: ${song.sound_name ?? 'unknown'} on ${platformLabel}`}
    >
      <div className="song-thumb">
        {song.thumbnail ? (
          <img src={song.thumbnail} alt={song.sound_name ?? 'cover'} loading="lazy" />
        ) : (
          <div className="song-thumb-placeholder"><span>♫</span></div>
        )}

        {selectable && (
          <label className="song-select" onClick={stop}>
            <input
              type="checkbox"
              checked={selected}
              onChange={() => onToggleSelect?.(song)}
              aria-label="Select sound for refresh"
            />
          </label>
        )}

        <span className="song-rank">{rankStr}</span>
        <span className="song-platform-badge" data-platform={song.platform}>{platformLabel}</span>
        <span className="song-note-icon" title="sound">♫</span>

        <div className="song-actions" onClick={stop}>
          {song.downloadable && (
            <a
              className="song-action"
              href={songAudioUrl(song.platform, song.key)}
              download
              title="Download audio"
              aria-label="Download audio"
            >
              ⬇
            </a>
          )}
          <button
            type="button"
            className={`song-action${song.pinned ? ' song-action--on' : ''}`}
            onClick={() => onTogglePin(song)}
            title={song.pinned ? 'Pinned — stays across refresh' : 'Pin (keep across refresh)'}
            aria-pressed={song.pinned}
          >
            📌
          </button>
          <button
            type="button"
            className={`song-action${song.hidden ? ' song-action--on' : ''}`}
            onClick={() => onToggleHide(song)}
            title={song.hidden ? 'Hidden — won’t appear' : 'Hide (don’t show me this)'}
            aria-pressed={song.hidden}
          >
            🚫
          </button>
        </div>
      </div>

      <div className="song-body">
        <div className="song-title-row">
          <span className="song-title" title={song.sound_name ?? undefined}>
            {song.sound_name ?? 'Unknown sound'}
          </span>
        </div>
        {song.sound_author && <span className="song-author">{song.sound_author}</span>}

        <div className="song-metrics">
          <span className="song-metric song-metric--primary" title={scoreLabel}>
            <span className="song-metric-val">{scoreValue}</span>
            <span className="song-metric-label">{scoreLabel}</span>
          </span>
          {activeSort !== 'reuse_count' && (
            <span
              className="song-metric"
              title={
                song.reuse_count_source === 'platform'
                  ? `${platformLabel}'s own count of videos using this sound`
                  : 'videos we’ve harvested using this sound (not yet pivoted for the platform count)'
              }
            >
              <span className="song-metric-val">
                {fmt(song.reuse_count)}
                {song.reuse_count_source === 'observed' ? '+' : ''}
              </span>
              <span className="song-metric-label">reuses</span>
            </span>
          )}
          {activeSort === 'reuse_count' && song.reuse_count_source === 'observed' && (
            <span className="song-metric song-metric--estimate" title="observed lower bound — not yet pivoted for the platform's authoritative count">
              <span className="song-metric-val">≈</span>
              <span className="song-metric-label">observed</span>
            </span>
          )}
          <span className="song-metric" title="distinct posts">
            <span className="song-metric-val">{fmt(song.post_count)}</span>
            <span className="song-metric-label">posts</span>
          </span>
          <span className="song-metric" title="distinct creators">
            <span className="song-metric-val">{fmt(song.distinct_accounts)}</span>
            <span className="song-metric-label">creators</span>
          </span>
          {song.total_views > 0 && (
            <span className="song-metric" title="total views">
              <span className="song-metric-val">{fmt(song.total_views)}</span>
              <span className="song-metric-label">views</span>
            </span>
          )}
          {song.rising > 0 && (
            <span className="song-metric song-metric--rising" title="posts first-seen recently">
              <span className="song-metric-val">↑{fmt(song.rising)}</span>
              <span className="song-metric-label">rising</span>
            </span>
          )}
        </div>
      </div>
    </article>
  );
}
