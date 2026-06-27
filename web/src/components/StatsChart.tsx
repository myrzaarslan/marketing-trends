import { useEffect, useMemo, useState } from 'react';
import { fetchSnapshots, resnapshotPost } from '../api';
import type { SnapshotPoint, SnapshotSeries } from '../types';
import './StatsChart.css';

interface Props {
  platform: string;
  platformPostId: string;
}

const W = 320;
const H = 120;
const PAD = 8;

const PALETTE = ['#6aa9ff', '#ff6a9a', '#ffc24b', '#5fd0a8', '#b88aff'];

function compact(n: number): string {
  const abs = Math.abs(n);
  if (abs >= 1e9) return (n / 1e9).toFixed(1).replace(/\.0$/, '') + 'B';
  if (abs >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
  if (abs >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'K';
  return String(Math.round(n));
}

function fmtRatio(n: number): string {
  if (n === 0) return '0';
  if (Math.abs(n) >= 1) return n.toFixed(2);
  return (n * 100).toPrecision(3) + '%';
}

function fmtTime(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

// One plotted line: a label, color, and a value (or null) per snapshot point.
interface Line {
  label: string;
  color: string;
  vals: (number | null)[];
}

type ModeId = 'raw' | 'er_views' | 'er_followers' | 'share_rate' | 'save_rate';
interface Mode {
  id: ModeId;
  label: string;
  ratio: boolean;
}

const MODES: Mode[] = [
  { id: 'raw', label: 'Raw counts', ratio: false },
  { id: 'er_views', label: 'Engagement ÷ views', ratio: true },
  { id: 'er_followers', label: 'Engagement ÷ followers', ratio: true },
  { id: 'share_rate', label: 'Share rate', ratio: true },
  { id: 'save_rate', label: 'Save rate', ratio: true },
];

function engagementSum(p: SnapshotPoint): number | null {
  const parts = [p.like_count, p.comment_count, p.share_count, p.save_count].filter(
    (v): v is number => v != null,
  );
  return parts.length ? parts.reduce((a, b) => a + b, 0) : null;
}

function ratio(num: number | null, den: number | null | undefined): number | null {
  return num != null && den != null && den > 0 ? num / den : null;
}

const RAW_METRICS: { key: keyof SnapshotPoint; label: string }[] = [
  { key: 'view_count', label: 'Views' },
  { key: 'like_count', label: 'Likes' },
  { key: 'comment_count', label: 'Comments' },
  { key: 'share_count', label: 'Shares' },
  { key: 'save_count', label: 'Saves' },
];

function linesForMode(mode: ModeId, pts: SnapshotPoint[]): Line[] {
  if (mode === 'raw') {
    return RAW_METRICS.map((m, i) => ({
      label: m.label,
      color: PALETTE[i % PALETTE.length],
      vals: pts.map((p) => p[m.key] as number | null),
    })).filter((l) => l.vals.some((v) => v != null));
  }
  // share/save rate prefer views, fall back to followers as the denominator.
  const rateDen = (p: SnapshotPoint) => p.view_count ?? p.author_follower_count;
  const spec: Record<Exclude<ModeId, 'raw'>, { label: string; color: string; val: (p: SnapshotPoint) => number | null }> = {
    er_views: { label: 'Engagement ÷ views', color: PALETTE[0], val: (p) => ratio(engagementSum(p), p.view_count) },
    er_followers: { label: 'Engagement ÷ followers', color: PALETTE[2], val: (p) => ratio(engagementSum(p), p.author_follower_count) },
    share_rate: { label: 'Share rate', color: PALETTE[3], val: (p) => ratio(p.share_count, rateDen(p)) },
    save_rate: { label: 'Save rate', color: PALETTE[4], val: (p) => ratio(p.save_count, rateDen(p)) },
  };
  const s = spec[mode];
  return [{ label: s.label, color: s.color, vals: pts.map(s.val) }];
}

export function StatsChart({ platform, platformPostId }: Props) {
  const [series, setSeries] = useState<SnapshotSeries | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<ModeId>('raw');
  const [refreshing, setRefreshing] = useState(false);
  const [refreshMsg, setRefreshMsg] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setSeries(null);
    setError(null);
    setRefreshMsg(null);
    fetchSnapshots(platform, platformPostId)
      .then((s) => { if (!cancelled) setSeries(s); })
      .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, [platform, platformPostId]);

  // Which modes have any plottable data (for enabling/disabling the chips).
  const modeAvailable = useMemo(() => {
    const out: Record<ModeId, boolean> = { raw: false, er_views: false, er_followers: false, share_rate: false, save_rate: false };
    if (series) {
      for (const m of MODES) {
        out[m.id] = linesForMode(m.id, series.points).some((l) => l.vals.some((v) => v != null));
      }
    }
    return out;
  }, [series]);

  async function onRefresh() {
    setRefreshing(true);
    setRefreshMsg(null);
    try {
      const prevLen = series?.points.length ?? 0;
      const res = await resnapshotPost(platform, platformPostId);
      setSeries(res.series);
      if (res.status === 'not_found') {
        setRefreshMsg('Post is no longer in the author’s recent feed — can’t re-observe.');
      } else if (res.status === 'error') {
        setRefreshMsg(res.error ?? 'Re-observation failed.');
      } else if (res.series.points.length > prevLen) {
        setRefreshMsg('Added a fresh data point just now.');
      } else {
        setRefreshMsg('Re-observed — no measurable change since the last point.');
      }
    } catch (e) {
      setRefreshMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setRefreshing(false);
    }
  }

  if (error) return <div className="stats-chart stats-chart-empty">Stats unavailable</div>;
  if (!series) return <div className="stats-chart stats-chart-empty">Loading stats…</div>;

  const pts = series.points;
  const n = pts.length;
  const activeMode = MODES.find((m) => m.id === mode)!;
  const lines = linesForMode(mode, pts);
  const hasData = lines.some((l) => l.vals.some((v) => v != null));

  const times = pts.map((p) => new Date(p.fetched_at).getTime());
  const t0 = times[0];
  const t1 = times[n - 1];
  const span = t1 - t0;
  const xAt = (i: number) =>
    n <= 1 || span <= 0
      ? PAD + (n <= 1 ? (W - 2 * PAD) / 2 : (i / (n - 1)) * (W - 2 * PAD))
      : PAD + ((times[i] - t0) / span) * (W - 2 * PAD);
  const plotH = H - 2 * PAD;

  function coordsFor(vals: (number | null)[]) {
    const present = vals.filter((v): v is number => v != null);
    const min = Math.min(...present);
    const max = Math.max(...present);
    const yAt = (v: number) => (max === min ? PAD + plotH / 2 : PAD + (1 - (v - min) / (max - min)) * plotH);
    return vals
      .map((v, i) => (v == null ? null : { x: xAt(i), y: yAt(v) }))
      .filter((c): c is { x: number; y: number } => c != null);
  }

  const fmtVal = (v: number) => (activeMode.ratio ? fmtRatio(v) : compact(v));

  return (
    <div className="stats-chart">
      <div className="stats-chart-head">
        <span className="stats-chart-title">Engagement over time</span>
        {series.velocity_per_hour != null && series.velocity_metric ? (
          <span className="stats-chart-velocity" title="Δ between the two most recent observations">
            {series.velocity_per_hour >= 0 ? '▲' : '▼'} {compact(Math.abs(series.velocity_per_hour))} {series.velocity_metric}/hr
          </span>
        ) : (
          <span className="stats-chart-velocity stats-chart-velocity-muted">
            velocity needs a 2nd observation
          </span>
        )}
      </div>

      {/* Metric selector */}
      <div className="stats-chart-modes">
        {MODES.map((m) => (
          <button
            key={m.id}
            type="button"
            className={`stats-chart-chip ${mode === m.id ? 'active' : ''}`}
            disabled={!modeAvailable[m.id]}
            title={modeAvailable[m.id] ? undefined : 'No data for this metric on this post'}
            onClick={() => setMode(m.id)}
          >
            {m.label}
          </button>
        ))}
      </div>

      {!hasData ? (
        <div className="stats-chart-single">This metric isn’t available for this post.</div>
      ) : n <= 1 ? (
        <div className="stats-chart-single">
          Only one observation so far. Use “Fetch fresh data” to add points over time.
        </div>
      ) : (
        <svg className="stats-chart-svg" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img">
          {lines.map((l) => {
            const coords = coordsFor(l.vals);
            const path = coords.map((c, i) => `${i === 0 ? 'M' : 'L'}${c.x.toFixed(1)},${c.y.toFixed(1)}`).join(' ');
            return (
              <g key={l.label}>
                <path d={path} fill="none" stroke={l.color} strokeWidth={1.75} />
                {coords.map((c, i) => (
                  <circle key={i} cx={c.x} cy={c.y} r={2.2} fill={l.color} />
                ))}
              </g>
            );
          })}
        </svg>
      )}

      <div className="stats-chart-axis">
        <span>{fmtTime(pts[0].fetched_at)}</span>
        {n > 1 && <span>{fmtTime(pts[n - 1].fetched_at)}</span>}
      </div>

      {hasData && (
        <ul className="stats-chart-legend">
          {lines.map((l) => {
            const latest = [...l.vals].reverse().find((v) => v != null) ?? null;
            const first = l.vals.find((v) => v != null) ?? null;
            const delta = latest != null && first != null ? latest - first : null;
            return (
              <li key={l.label}>
                <span className="stats-chart-swatch" style={{ background: l.color }} />
                <span className="stats-chart-label">{l.label}</span>
                <span className="stats-chart-value">{latest != null ? fmtVal(latest) : '—'}</span>
                {delta != null && delta !== 0 && (
                  <span className={`stats-chart-delta ${delta > 0 ? 'up' : 'down'}`}>
                    {delta > 0 ? '+' : '−'}{fmtVal(Math.abs(delta))}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {/* Fetch-fresh control */}
      <div className="stats-chart-refresh">
        <span className="stats-chart-asof">as of {fmtTime(pts[n - 1].fetched_at)}</span>
        <button type="button" className="stats-chart-refresh-btn" onClick={onRefresh} disabled={refreshing}>
          {refreshing ? 'Fetching…' : 'Fetch fresh data'}
        </button>
      </div>
      {refreshMsg && <div className="stats-chart-refresh-msg">{refreshMsg}</div>}
    </div>
  );
}
