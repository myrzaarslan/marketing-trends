import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchDigest, fetchDigestMeta, fetchRefreshStatus, triggerRefresh } from './api';
import { DigestCard } from './components/DigestCard';
import { FilterBar } from './components/FilterBar';
import { PostLightbox } from './components/PostLightbox';
import type { DigestCard as DigestCardType, DigestFilters, DigestMeta } from './types';
import './App.css';

const DEFAULT_FILTERS: DigestFilters = {
  platform: '',
  geo: '',
  period: 30,
  sort: 'engagement_rate',
  limit: 50,
};

export default function App() {
  const [filters, setFilters] = useState<DigestFilters>(DEFAULT_FILTERS);
  const [cards, setCards] = useState<DigestCardType[]>([]);
  const [meta, setMeta] = useState<DigestMeta | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshStatus, setRefreshStatus] = useState<string | null>(null);
  const [totalCount, setTotalCount] = useState(0);

  // Lightbox state
  const [openCard, setOpenCard] = useState<DigestCardType | null>(null);
  const [openRank, setOpenRank] = useState(1);

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const loadDigest = useCallback(async (f: DigestFilters) => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchDigest(f);
      setCards(data.cards);
      setTotalCount(data.count);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadMeta = useCallback(async (platform: string) => {
    try {
      const m = await fetchDigestMeta(platform || undefined);
      setMeta(m);
    } catch {
      // non-fatal
    }
  }, []);

  useEffect(() => {
    loadDigest(filters);
    loadMeta(filters.platform);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const handleFiltersChange = (next: DigestFilters) => {
    setFilters(next);
    loadDigest(next);
    loadMeta(next.platform);
  };

  const handleRefresh = async () => {
    setRefreshStatus('queued');
    try {
      const { job_id } = await triggerRefresh();
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = setInterval(async () => {
        try {
          const status = await fetchRefreshStatus(job_id);
          setRefreshStatus(status.status);
          if (status.status === 'done' || status.status === 'error') {
            clearInterval(pollRef.current!);
            pollRef.current = null;
            if (status.status === 'done') {
              await loadDigest(filters);
            }
          }
        } catch {
          clearInterval(pollRef.current!);
          pollRef.current = null;
          setRefreshStatus('error');
        }
      }, 2000);
    } catch {
      setRefreshStatus('error');
    }
  };

  const handleOpenCard = useCallback((card: DigestCardType) => {
    const rank = cards.findIndex(
      (c) => c.platform === card.platform && c.platform_post_id === card.platform_post_id
    ) + 1;
    setOpenCard(card);
    setOpenRank(rank || 1);
  }, [cards]);

  const handleCloseCard = useCallback(() => {
    setOpenCard(null);
  }, []);

  const refreshRunning = refreshStatus === 'queued' || refreshStatus === 'running';

  return (
    <div className="app">
      <header className="app-header">
        <div className="header-left">
          <span className="header-wordmark">TREND INTELLIGENCE</span>
          <span className="header-sep">·</span>
          <span className="header-subtitle">EdTech KZ/CIS specimen viewer</span>
        </div>
        <div className="header-right">
          {refreshStatus && (
            <span className={`refresh-badge refresh-${refreshStatus}`}>
              {refreshStatus === 'queued' && '⟳ Queued'}
              {refreshStatus === 'running' && '⟳ Ingesting…'}
              {refreshStatus === 'done' && '✓ Updated'}
              {refreshStatus === 'error' && '✕ Error'}
            </span>
          )}
          <button
            className="btn-refresh"
            onClick={handleRefresh}
            disabled={refreshRunning}
          >
            {refreshRunning ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </header>

      <FilterBar
        filters={filters}
        meta={meta}
        onChange={handleFiltersChange}
      />

      <main className="digest-main">
        {error && (
          <div className="error-banner">
            <strong>Error:</strong> {error}
          </div>
        )}

        {loading && (
          <div className="loading">Scanning specimens…</div>
        )}

        {!loading && !error && (
          <>
            <div className="result-meta">
              <span className="result-count">{totalCount.toLocaleString()}</span>
              {' specimens'}
              {filters.platform && <span className="result-filter"> · {filters.platform}</span>}
              {filters.geo && <span className="result-filter"> · {filters.geo}</span>}
              <span className="result-filter"> · {filters.period}d window</span>
              <span className="result-filter"> · by {filters.sort.replace(/_/g, ' ')}</span>
            </div>
            {cards.length === 0 ? (
              <div className="empty-state">
                No specimens found for these filters.
                <br />
                <span className="empty-hint">Try a wider period or remove platform/geo filters.</span>
              </div>
            ) : (
              <div className="card-grid">
                {cards.map((card, i) => (
                  <DigestCard
                    key={`${card.platform}:${card.platform_post_id}`}
                    card={card}
                    rank={i + 1}
                    onOpen={handleOpenCard}
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
        />
      )}
    </div>
  );
}
