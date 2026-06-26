import type { DigestCard as DigestCardType } from '../types';
import { profileUrl } from '../platformLinks';
import './DigestCard.css';

interface Props {
  card: DigestCardType;
  rank: number;
  onOpen: (card: DigestCardType) => void;
}

const PLATFORM_LABELS: Record<string, string> = {
  tiktok: 'TikTok',
  instagram: 'Instagram',
  x: 'X',
  threads: 'Threads',
};

const MEDIA_TYPE_ICONS: Record<string, string> = {
  video: '▶',
  image: '⬛',
  text: '≡',
};

function fmt(n: number | null | undefined): string {
  if (n == null) return '–';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function fmtScore(score: number | null): string {
  if (score == null) return '–';
  if (score > 1000) return fmt(Math.round(score));
  if (score > 1) return score.toFixed(1);
  return score.toFixed(4);
}

function timeAgo(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  const h = Math.floor(ms / 3600000);
  if (h < 24) return `${h}h`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d`;
  return `${Math.floor(d / 30)}mo`;
}

export function DigestCard({ card, rank, onOpen }: Props) {
  const platformLabel = PLATFORM_LABELS[card.platform] ?? card.platform;
  const rankStr = `#${String(rank).padStart(3, '0')}`;
  const degraded = card.sort_used !== card.sort_requested;

  // Prefer the resolved thumbnail URL; fall back to thumbnail_path
  const thumbnailSrc = card.thumbnail
    ?? (card.thumbnail_path
      ? `/thumbnails/${card.platform}/${card.thumbnail_path.split('/').pop()}`
      : null);

  const canOpen = card.has_content_bundle || card.has_content;
  const mediaIcon = MEDIA_TYPE_ICONS[card.media_type] ?? '·';

  return (
    <article
      className={`digest-card${canOpen ? ' digest-card--clickable' : ''}`}
      data-platform={card.platform}
      onClick={() => onOpen(card)}
      onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && onOpen(card)}
      tabIndex={0}
      role="button"
      aria-label={`View specimen ${rankStr}: @${card.account_handle} on ${platformLabel}`}
    >
      {/* Thumbnail */}
      <div className="card-thumb">
        {thumbnailSrc ? (
          <img src={thumbnailSrc} alt={card.caption ?? 'thumbnail'} loading="lazy" />
        ) : (
          <div className="card-thumb-placeholder">
            <span>{platformLabel[0]}</span>
          </div>
        )}

        {/* Rank chip — the specimen label */}
        <span className="card-rank">{rankStr}</span>

        {/* Platform badge */}
        <span className="card-platform-badge" data-platform={card.platform}>
          {platformLabel}
        </span>

        {/* Media type indicator */}
        <span className="card-media-type" title={card.media_type}>
          {mediaIcon}
        </span>

        {/* Content bundle indicator */}
        {canOpen && (
          <span className="card-bundle-badge" title="Full content bundle — click to open">
            ◉
          </span>
        )}
      </div>

      <div className="card-body">
        {/* Handle + geo */}
        <div className="card-handle">
          {(() => {
            const href = profileUrl(card.platform, card.account_handle);
            return href ? (
              <a
                className="card-handle-text card-handle-link"
                href={href}
                target="_blank"
                rel="noopener noreferrer"
                onClick={(e) => e.stopPropagation()}
                title={`Open @${card.account_handle} on ${PLATFORM_LABELS[card.platform] ?? card.platform}`}
              >
                @{card.account_handle}
              </a>
            ) : (
              <span className="card-handle-text">@{card.account_handle}</span>
            );
          })()}
          {card.geo_tier && <span className="card-geo">{card.geo_tier}</span>}
        </div>

        {/* Caption */}
        {card.caption && (
          <p className="card-caption">
            {card.caption.slice(0, 100)}{card.caption.length > 100 ? '…' : ''}
          </p>
        )}

        {/* Hashtags */}
        {card.hashtags.length > 0 && (
          <div className="card-hashtags">
            {card.hashtags.slice(0, 4).map((h) => (
              <span key={h} className="card-hashtag">#{h}</span>
            ))}
          </div>
        )}

        {/* Stats — only non-null values (platform-honoured) */}
        <div className="card-stats">
          {card.view_count != null && (
            <span title="Views"><span className="stat-icon">👁</span>{fmt(card.view_count)}</span>
          )}
          {card.like_count != null && (
            <span title="Likes"><span className="stat-icon">♥</span>{fmt(card.like_count)}</span>
          )}
          {card.comment_count != null && (
            <span title="Comments"><span className="stat-icon">💬</span>{fmt(card.comment_count)}</span>
          )}
          {card.share_count != null && (
            <span title="Shares"><span className="stat-icon">↗</span>{fmt(card.share_count)}</span>
          )}
          {card.save_count != null && (
            <span title="Saves"><span className="stat-icon">🔖</span>{fmt(card.save_count)}</span>
          )}
        </div>

        {/* Footer: score + age */}
        <div className="card-footer">
          <div
            className="card-score"
            title={`${card.sort_used}${degraded ? ` (degraded from ${card.sort_requested})` : ''}`}
          >
            <span className="score-label">{card.sort_used.replace(/_/g, ' ')}</span>
            <span className="score-value">{fmtScore(card.score)}</span>
            {degraded && (
              <span className="score-degraded" title={`Requested ${card.sort_requested}`}>↘</span>
            )}
          </div>
          <div className="card-meta-right">
            {card.snapshot_days < 3 && (
              <span className="card-history-gate" title={`${card.snapshot_days} snapshot day(s) — needs ≥3 for history sorts`}>
                {card.snapshot_days}d
              </span>
            )}
            <span className="card-age" title={`First seen: ${card.first_seen_at}`}>
              {timeAgo(card.first_seen_at)}
            </span>
          </div>
        </div>
      </div>
    </article>
  );
}
