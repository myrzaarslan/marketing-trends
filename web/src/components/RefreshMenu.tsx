import { useEffect, useRef, useState } from 'react';
import './RefreshMenu.css';

interface Props {
  running: boolean;
  selectionMode: boolean;
  selectedCount: number;
  onSoft: () => void;
  onHardCorpus: () => void;
  onHardLive: () => void;
  onToggleSelectionMode: () => void;
  onRefreshSelected: () => void;
}

export function RefreshMenu({
  running,
  selectionMode,
  selectedCount,
  onSoft,
  onHardCorpus,
  onHardLive,
  onToggleSelectionMode,
  onRefreshSelected,
}: Props) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, []);

  const pick = (fn: () => void) => () => { setOpen(false); fn(); };

  return (
    <div className="refresh-menu" ref={ref}>
      <button className="btn-refresh" onClick={() => setOpen((v) => !v)} disabled={running}>
        {running ? 'Refreshing…' : 'Refresh ▾'}
      </button>

      {open && (
        <div className="refresh-dropdown" role="menu">
          <button type="button" className="refresh-opt" onClick={pick(onSoft)}>
            <span className="refresh-opt-title">↻ Soft refresh</span>
            <span className="refresh-opt-sub">Re-rank & re-enrich the current set</span>
          </button>
          <button type="button" className="refresh-opt" onClick={pick(onHardCorpus)}>
            <span className="refresh-opt-title">✦ Hard refresh — corpus</span>
            <span className="refresh-opt-sub">Fresh unseen set from existing posts (fast)</span>
          </button>
          <button type="button" className="refresh-opt" onClick={pick(onHardLive)}>
            <span className="refresh-opt-title">🛰 Hard refresh — live</span>
            <span className="refresh-opt-sub">Harvest brand-new posts from platforms (slow)</span>
          </button>

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
