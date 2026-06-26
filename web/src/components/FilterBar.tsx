import type { DigestFilters, DigestMeta, SortKey } from '../types';
import './FilterBar.css';

interface Props {
  filters: DigestFilters;
  meta: DigestMeta | null;
  onChange: (filters: DigestFilters) => void;
}

const SORT_LABELS: Record<SortKey, string> = {
  engagement_rate: 'Engagement Rate (÷ views)',
  engagement_rate_followers: 'Engagement Rate (÷ followers)',
  raw_counts: 'Raw Counts',
  share_rate: 'Share Rate',
  save_rate: 'Save Rate',
  velocity: 'Velocity',
  relative_baseline: 'Relative to Baseline',
  cross_persona: 'Cross-Persona Breadth',
};

const SORT_TIPS: Record<SortKey, string> = {
  engagement_rate: '(likes+comments+shares+saves) / views — default (no views on Threads)',
  engagement_rate_followers: '(likes+comments+shares+saves) / follower count — works on all platforms',
  raw_counts: 'Total engagement count sum',
  share_rate: 'Shares / views — TikTok / X / Threads only',
  save_rate: 'Saves / views — TikTok only',
  velocity: 'Δ views per hour between snapshots — needs ≥2 snapshots over time',
  relative_baseline: 'Views vs creator median — needs ≥3 of the account\'s posts (not time)',
  cross_persona: 'Distinct sources that surfaced this post — needs ≥2 sources',
};

const ALL_SORTS: SortKey[] = [
  'engagement_rate',
  'engagement_rate_followers',
  'raw_counts',
  'share_rate',
  'save_rate',
  'velocity',
  'relative_baseline',
  'cross_persona',
];

const PLATFORMS = ['', 'tiktok', 'instagram', 'x', 'threads'];
const GEOS = ['', 'KZ', 'CIS', 'World'];
const PERIODS = [7, 14, 30, 60, 90];

export function FilterBar({ filters, meta, onChange }: Props) {
  const sortAvail = meta?.sort_availability ?? null;

  const update = (patch: Partial<DigestFilters>) => {
    const next = { ...filters, ...patch };
    // If selected sort is now unavailable, fall back to default
    if (sortAvail && !sortAvail[next.sort]) {
      next.sort = 'engagement_rate';
    }
    onChange(next);
  };

  const isSortAvail = (key: SortKey): boolean => {
    if (!sortAvail) return true; // unknown — allow optimistically
    return sortAvail[key] ?? false;
  };

  return (
    <div className="filter-bar">
      <div className="filter-group">
        <label>Platform</label>
        <select
          value={filters.platform}
          onChange={(e) => update({ platform: e.target.value })}
        >
          <option value="">All</option>
          {PLATFORMS.filter(Boolean).map((p) => (
            <option key={p} value={p}>{p}</option>
          ))}
        </select>
      </div>

      <div className="filter-group">
        <label>Geo</label>
        <select
          value={filters.geo}
          onChange={(e) => update({ geo: e.target.value })}
        >
          <option value="">All</option>
          {GEOS.filter(Boolean).map((g) => (
            <option key={g} value={g}>{g}</option>
          ))}
        </select>
      </div>

      <div className="filter-group">
        <label>Period</label>
        <select
          value={filters.period}
          onChange={(e) => update({ period: Number(e.target.value) })}
        >
          {PERIODS.map((d) => (
            <option key={d} value={d}>{d}d</option>
          ))}
        </select>
      </div>

      <div className="filter-group sort-group">
        <label>Sort</label>
        <select
          value={filters.sort}
          onChange={(e) => update({ sort: e.target.value as SortKey })}
        >
          {ALL_SORTS.map((key) => {
            const avail = isSortAvail(key);
            return (
              <option key={key} value={key} disabled={!avail}>
                {SORT_LABELS[key]}{!avail ? ' ⊘' : ''}
              </option>
            );
          })}
        </select>
        <span className="sort-tip" title={SORT_TIPS[filters.sort]}>ⓘ</span>
      </div>

      <div className="filter-group">
        <label>Limit</label>
        <select
          value={filters.limit}
          onChange={(e) => update({ limit: Number(e.target.value) })}
        >
          {[25, 50, 100, 200].map((n) => (
            <option key={n} value={n}>{n}</option>
          ))}
        </select>
      </div>
    </div>
  );
}
