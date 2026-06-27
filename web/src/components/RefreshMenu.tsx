import { useEffect, useRef, useState } from 'react';
import './RefreshMenu.css';

export interface RefreshOpts {
  /** How many posts the refresh returns (working-set / page size). */
  count: number;
  /** Platforms to refresh; empty = all. */
  platforms: string[];
  /** New posts to scrape per platform on a live harvest. */
  liveDepth: number;
}

interface Props {
  running: boolean;
  selectionMode: boolean;
  selectedCount: number;
  /** Initial post count (defaults to the current digest limit). */
  defaultCount: number;
  onSoft: () => void;
  onHardCorpus: (opts: RefreshOpts) => void;
  onHardLive: (opts: RefreshOpts) => void;
  onToggleSelectionMode: () => void;
  onRefreshSelected: () => void;
}

const ALL_PLATFORMS = ['tiktok', 'instagram', 'x', 'threads'] as const;
const LIVE_DEPTHS = [25, 100, 250, 500];

export function RefreshMenu({
  running,
  selectionMode,
  selectedCount,
  defaultCount,
  onSoft,
  onHardCorpus,
  onHardLive,
  onToggleSelectionMode,
  onRefreshSelected,
}: Props) {
  const [open, setOpen] = useState(false);
  // Raw string so the field can be temporarily empty without collapsing to 1.
  const [countStr, setCountStr] = useState(String(defaultCount));
  const [platforms, setPlatforms] = useState<string[]>([...ALL_PLATFORMS]);
  const [liveDepth, setLiveDepth] = useState(500);
  const ref = useRef<HTMLDivElement>(null);

  // Track the filter-bar limit while the user hasn't typed their own value.
  useEffect(() => {
    setCountStr(String(defaultCount));
  }, [defaultCount]);

  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, []);

  const togglePlatform = (p: string) =>
    setPlatforms((cur) =>
      cur.includes(p) ? cur.filter((x) => x !== p) : [...cur, p],
    );

  // All four selected = "all" (send empty so the backend takes the unscoped path).
  const effectivePlatforms = platforms.length === ALL_PLATFORMS.length ? [] : platforms;
  // Empty / invalid field falls back to the filter-bar limit, never to 1.
  const parsed = parseInt(countStr, 10);
  const safeCount = Math.max(1, Math.min(200, Number.isFinite(parsed) && parsed > 0 ? parsed : defaultCount));
  const opts = (): RefreshOpts => ({
    count: safeCount,
    platforms: effectivePlatforms,
    liveDepth,
  });

  const pick = (fn: () => void) => () => { setOpen(false); fn(); };

  return (
    <div className="refresh-menu" ref={ref}>
      <button className="btn-refresh" onClick={() => setOpen((v) => !v)} disabled={running}>
        {running ? 'Refreshing…' : 'Refresh ▾'}
      </button>

      {open && (
        <div className="refresh-dropdown" role="menu">
          {/* Shared options for both hard-refresh modes */}
          <div className="refresh-config" onClick={(e) => e.stopPropagation()}>
            <label className="refresh-row">
              <span className="refresh-config-label">Posts to fetch</span>
              <input
                type="number"
                min={1}
                max={200}
                value={countStr}
                onChange={(e) => setCountStr(e.target.value)}
                onBlur={() => setCountStr(String(safeCount))}
                className="refresh-count-input"
              />
            </label>

            <div className="refresh-row refresh-row--col">
              <span className="refresh-config-label">Platforms</span>
              <div className="refresh-platforms">
                {ALL_PLATFORMS.map((p) => (
                  <button
                    key={p}
                    type="button"
                    className={`refresh-plat-chip ${platforms.includes(p) ? 'active' : ''}`}
                    onClick={() => togglePlatform(p)}
                  >
                    {p}
                  </button>
                ))}
              </div>
              <span className="refresh-hint">
                {platforms.length === 0
                  ? 'Pick at least one platform'
                  : platforms.length === ALL_PLATFORMS.length
                    ? 'All platforms'
                    : platforms.join(' + ')}
              </span>
            </div>
          </div>

          <div className="refresh-sep" />

          <button type="button" className="refresh-opt" onClick={pick(onSoft)}>
            <span className="refresh-opt-title">↻ Soft refresh</span>
            <span className="refresh-opt-sub">Re-rank & re-enrich the current set</span>
          </button>
          <button
            type="button"
            className="refresh-opt"
            disabled={platforms.length === 0}
            onClick={pick(() => onHardCorpus(opts()))}
          >
            <span className="refresh-opt-title">✦ Hard refresh — corpus</span>
            <span className="refresh-opt-sub">
              {safeCount} fresh unseen posts from existing corpus (fast)
            </span>
          </button>
          <button
            type="button"
            className="refresh-opt"
            disabled={platforms.length === 0}
            onClick={pick(() => onHardLive(opts()))}
          >
            <span className="refresh-opt-title">🛰 Hard refresh — live</span>
            <span className="refresh-opt-sub">
              Scrape {liveDepth} new/platform, show {safeCount} (slow; 500 ≈ minutes)
            </span>
          </button>
          <div className="refresh-depth" onClick={(e) => e.stopPropagation()}>
            <span className="refresh-depth-label">Live scrape depth / platform:</span>
            {LIVE_DEPTHS.map((d) => (
              <button
                key={d}
                type="button"
                className={`refresh-depth-chip ${liveDepth === d ? 'active' : ''}`}
                onClick={() => setLiveDepth(d)}
              >
                {d}
              </button>
            ))}
          </div>

          <div className="refresh-sep" />

          <button type="button" className="refresh-opt" onClick={pick(onToggleSelectionMode)}>
            <span className="refresh-opt-title">
              {selectionMode ? '✓ Selecting…' : '☑ Select videos to refresh'}
            </span>
            <span className="refresh-opt-sub">
              {selectionMode ? 'Pick cards, then “Refresh selected”' : 'Choose individual cards to rotate out'}
            </span>
          </button>
          {selectionMode && (
            <button
              type="button"
              className="refresh-opt refresh-opt--primary"
              onClick={pick(onRefreshSelected)}
              disabled={selectedCount === 0}
            >
              <span className="refresh-opt-title">→ Refresh {selectedCount} selected</span>
              <span className="refresh-opt-sub">Replace selected with fresh unseen posts</span>
            </button>
          )}
        </div>
      )}
    </div>
  );
}
