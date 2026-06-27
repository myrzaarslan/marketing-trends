import { useCallback, useEffect, useRef, useState } from 'react';
import {
  addToCollection,
  createCollection,
  deleteCollection,
  deleteNote,
  enrichPost,
  fetchDigest,
  fetchDigestMeta,
  fetchRefreshStatus,
  getCollection,
  listCollections,
  putFlags,
  putNote,
  removeFromCollection,
  triggerHardRefresh,
  triggerRefresh,
  updateCollection,
} from './api';
import { DigestCard } from './components/DigestCard';
import { CollectionsBar } from './components/CollectionsBar';
import { RefreshMenu } from './components/RefreshMenu';
import type { RefreshOpts } from './components/RefreshMenu';
import { FilterBar } from './components/FilterBar';
import { PostLightbox } from './components/PostLightbox';
import { SongsView } from './components/SongsView';
import type {
  Collection,
  CollectionDetail,
  DigestCard as DigestCardType,
  DigestFilters,
  DigestMeta,
  RefreshSource,
} from './types';
import './App.css';

const DEFAULT_FILTERS: DigestFilters = {
  platform: '',
  geo: '',
  period: 30,
  sort: 'engagement_rate',
  limit: 50,
  unseen_only: false,
};

const keyOf = (c: { platform: string; platform_post_id: string }) =>
  `${c.platform}:${c.platform_post_id}`;

export default function App() {
  const [filters, setFilters] = useState<DigestFilters>(DEFAULT_FILTERS);
  const [cards, setCards] = useState<DigestCardType[]>([]);
  const [meta, setMeta] = useState<DigestMeta | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshStatus, setRefreshStatus] = useState<string | null>(null);
  const [totalCount, setTotalCount] = useState(0);

  // Top-level section: the post digest, or the Sounds (viral songs) view.
  const [section, setSection] = useState<'digest' | 'songs'>('digest');

  // Collections
  const [collections, setCollections] = useState<Collection[]>([]);
  const [activeCollection, setActiveCollection] = useState<CollectionDetail | null>(null);
  const activeId = activeCollection?.id ?? null;

  // Selective refresh
  const [selectionMode, setSelectionMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  // Lightbox
  const [openCard, setOpenCard] = useState<DigestCardType | null>(null);
  const [openRank, setOpenRank] = useState(1);

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // -- loaders ---------------------------------------------------------------

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
      setMeta(await fetchDigestMeta(platform || undefined));
    } catch { /* non-fatal */ }
  }, []);

  const loadCollections = useCallback(async () => {
    try {
      const { collections } = await listCollections();
      setCollections(collections);
    } catch { /* non-fatal */ }
  }, []);

  const openCollection = useCallback(async (id: number) => {
    setSection('digest');
    setLoading(true);
    setError(null);
    try {
      setActiveCollection(await getCollection(id));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  const goHome = useCallback(() => {
    setSection('digest');
    setActiveCollection(null);
    loadDigest(filters);
  }, [filters, loadDigest]);

  const goSongs = useCallback(() => {
    setSection('songs');
    setActiveCollection(null);
  }, []);

  useEffect(() => {
    loadDigest(filters);
    loadMeta(filters.platform);
    loadCollections();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // -- card state patching ---------------------------------------------------

  const patchCard = useCallback((k: string, partial: Partial<DigestCardType>) => {
    setCards((cs) => cs.map((c) => (keyOf(c) === k ? { ...c, ...partial } : c)));
    setActiveCollection((ac) =>
      ac ? { ...ac, cards: ac.cards.map((c) => (keyOf(c) === k ? { ...c, ...partial } : c)) } : ac,
    );
    setOpenCard((oc) => (oc && keyOf(oc) === k ? { ...oc, ...partial } : oc));
  }, []);

  // When the lightbox finishes on-demand enrichment, reflect the new thumbnail
  // on the card so closing the lightbox shows downloaded media immediately.
  const onEnriched = useCallback((card: DigestCardType, thumbnail: string | null) => {
    patchCard(keyOf(card), { thumbnail, has_content_bundle: true, has_content: true });
  }, [patchCard]);

  // -- filters ---------------------------------------------------------------

  const handleFiltersChange = (next: DigestFilters) => {
    setFilters(next);
    if (activeId === null) loadDigest(next);
    loadMeta(next.platform);
  };

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

  const handleSoftRefresh = async () => {
    try {
      const { job_id } = await triggerRefresh();
      pollJob(job_id, () => loadDigest(filters));
    } catch {
      setRefreshStatus('error');
    }
  };

  const runHardRefresh = async (
    source: RefreshSource,
    serveIds: [string, string][],
    opts?: { count?: number; platforms?: string[]; liveDepth?: number },
  ) => {
    const count = opts?.count ?? filters.limit;
    const platforms = opts?.platforms ?? [];
    // For a live harvest, never scrape fewer than we intend to show.
    const liveDepth =
      opts?.liveDepth != null ? Math.max(opts.liveDepth, count) : undefined;
    try {
      const { job_id } = await triggerHardRefresh({
        source,
        serve_ids: serveIds,
        platform: filters.platform || null,
        platforms: platforms.length ? platforms : null,
        geo: filters.geo || null,
        period: filters.period,
        sort: filters.sort,
        limit: count,
        ...(liveDepth != null ? { live_per_platform: liveDepth } : {}),
      });
      pollJob(job_id, () => {
        // Reflect the chosen scope in the reloaded working set: a multi-platform
        // pick uses `platforms`; otherwise keep the single-platform filter bar.
        const next: typeof filters = {
          ...filters,
          limit: count,
          platforms: platforms.length ? platforms : undefined,
          platform: platforms.length ? '' : filters.platform,
          unseen_only: true,
        };
        setFilters(next);
        setActiveCollection(null);
        loadDigest(next);
        loadCollections();
        setSelected(new Set());
        setSelectionMode(false);
      });
    } catch {
      setRefreshStatus('error');
    }
  };

  const currentServeIds = (): [string, string][] =>
    cards.map((c) => [c.platform, c.platform_post_id] as [string, string]);

  const handleHardCorpus = (opts: RefreshOpts) =>
    runHardRefresh('corpus', currentServeIds(), opts);
  const handleHardLive = (opts: RefreshOpts) =>
    runHardRefresh('live', currentServeIds(), opts);

  const handleRefreshSelected = () => {
    const ids = cards
      .filter((c) => selected.has(keyOf(c)))
      .map((c) => [c.platform, c.platform_post_id] as [string, string]);
    if (ids.length) runHardRefresh('corpus', ids);
  };

  const showAll = () => {
    const next = { ...filters, unseen_only: false };
    setFilters(next);
    loadDigest(next);
  };

  // -- card actions ----------------------------------------------------------

  const onTogglePin = async (card: DigestCardType) => {
    const pinned = !card.pinned;
    patchCard(keyOf(card), { pinned });
    try { await putFlags(card.platform, card.platform_post_id, { pinned }); }
    catch { patchCard(keyOf(card), { pinned: !pinned }); }
  };

  const onToggleHide = async (card: DigestCardType) => {
    const hidden = !card.hidden;
    try {
      await putFlags(card.platform, card.platform_post_id, { hidden });
      if (hidden && activeId === null) {
        // Hidden posts leave the digest immediately.
        setCards((cs) => cs.filter((c) => keyOf(c) !== keyOf(card)));
        setTotalCount((n) => Math.max(0, n - 1));
      } else {
        patchCard(keyOf(card), { hidden });
      }
    } catch { /* keep state */ }
  };

  const onSaveNote = async (card: DigestCardType, body: string) => {
    const prev = card.note ?? null;
    patchCard(keyOf(card), { note: body || null });
    try {
      if (body) await putNote(card.platform, card.platform_post_id, body);
      else await deleteNote(card.platform, card.platform_post_id);
    } catch { patchCard(keyOf(card), { note: prev }); }
  };

  const onCreateCollection = async (title: string): Promise<Collection | null> => {
    try {
      const c = await createCollection(title);
      setCollections((cs) => [c, ...cs]);
      return c;
    } catch { return null; }
  };

  const onToggleCollection = async (
    card: DigestCardType,
    collectionId: number,
    makeMember: boolean,
  ) => {
    const ids = new Set(card.collection_ids ?? []);
    if (makeMember) ids.add(collectionId); else ids.delete(collectionId);
    patchCard(keyOf(card), { collection_ids: [...ids] });
    setCollections((cs) =>
      cs.map((c) => (c.id === collectionId
        ? { ...c, item_count: c.item_count + (makeMember ? 1 : -1) }
        : c)),
    );
    try {
      if (makeMember) await addToCollection(collectionId, card.platform, card.platform_post_id);
      else await removeFromCollection(collectionId, card.platform, card.platform_post_id);
      // If we're viewing the affected collection, reflect membership change.
      if (activeId === collectionId && !makeMember) {
        setActiveCollection((ac) =>
          ac ? { ...ac, cards: ac.cards.filter((c) => keyOf(c) !== keyOf(card)) } : ac);
      }
    } catch { /* best-effort; counts may drift until reload */ }
  };

  const onRemoveFromCollection = async (card: DigestCardType) => {
    if (activeId === null) return;
    setActiveCollection((ac) =>
      ac ? { ...ac, cards: ac.cards.filter((c) => keyOf(c) !== keyOf(card)), item_count: ac.item_count - 1 } : ac);
    setCollections((cs) =>
      cs.map((c) => (c.id === activeId ? { ...c, item_count: Math.max(0, c.item_count - 1) } : c)));
    try { await removeFromCollection(activeId, card.platform, card.platform_post_id); }
    catch { /* reload would resync */ }
  };

  const onRenameCollection = async () => {
    if (!activeCollection) return;
    const title = window.prompt('Rename collection', activeCollection.title);
    if (title == null) return;
    const updated = await updateCollection(activeCollection.id, { title });
    setActiveCollection((ac) => (ac ? { ...ac, title: updated.title } : ac));
    setCollections((cs) => cs.map((c) => (c.id === updated.id ? { ...c, title: updated.title } : c)));
  };

  const onDeleteCollection = async () => {
    if (!activeCollection) return;
    if (!window.confirm(`Delete collection “${activeCollection.title}”? Saved posts stay in the corpus.`)) return;
    await deleteCollection(activeCollection.id);
    setCollections((cs) => cs.filter((c) => c.id !== activeCollection.id));
    setActiveCollection(null);
    loadDigest(filters);
  };

  // -- selection -------------------------------------------------------------

  const onToggleSelect = (card: DigestCardType) => {
    setSelected((s) => {
      const next = new Set(s);
      const k = keyOf(card);
      if (next.has(k)) next.delete(k); else next.add(k);
      return next;
    });
  };

  // -- lightbox --------------------------------------------------------------

  const viewCards = activeId === null ? cards : (activeCollection?.cards ?? []);

  // Background pre-download: walk the visible list and enrich any card whose media
  // isn't downloaded yet, one at a time with gentle pacing, so thumbnails fill in
  // without the user having to open each post. Attempted keys are remembered so we
  // never re-hit the same post; the lightbox's on-demand enrich still takes priority.
  const enrichAttempted = useRef<Set<string>>(new Set());
  useEffect(() => {
    let cancelled = false;
    const queue = viewCards.filter(
      (c) => !c.has_content_bundle && !enrichAttempted.current.has(keyOf(c)),
    );
    if (queue.length === 0) return;
    (async () => {
      for (const card of queue) {
        if (cancelled) return;
        const k = keyOf(card);
        enrichAttempted.current.add(k);
        try {
          const nb = await enrichPost(card.platform, card.platform_post_id);
          if (cancelled) return;
          if (nb.enriched) {
            patchCard(k, {
              thumbnail: nb.thumbnail ?? null,
              has_content_bundle: true,
              has_content: true,
            });
          }
        } catch {
          enrichAttempted.current.delete(k); // allow a retry on next load
        }
        await new Promise((r) => setTimeout(r, 250));
      }
    })();
    return () => { cancelled = true; };
  }, [viewCards, patchCard]);

  const handleOpenCard = (card: DigestCardType) => {
    const rank = viewCards.findIndex((c) => keyOf(c) === keyOf(card)) + 1;
    setOpenCard(card);
    setOpenRank(rank || 1);
  };

  const handleCloseCard = useCallback(() => setOpenCard(null), []);

  const refreshRunning = refreshStatus === 'queued' || refreshStatus === 'running';

  const cardProps = (inCollection: boolean) => ({
    collections,
    onTogglePin,
    onToggleHide,
    onToggleCollection,
    onCreateCollection,
    onSaveNote,
    ...(inCollection ? { onRemoveFromCollection } : {}),
  });

  return (
    <div className="app">
      <header className="app-header">
        <div className="header-left">
          <span className="header-wordmark">TREND INTELLIGENCE</span>
          <span className="header-sep">·</span>
          <span className="header-subtitle">EdTech KZ/CIS specimen viewer</span>
        </div>
        <div className="header-right">
          {section === 'digest' && (
            <>
              {refreshStatus && (
                <span className={`refresh-badge refresh-${refreshStatus}`}>
                  {refreshStatus === 'queued' && '⟳ Queued'}
                  {refreshStatus === 'running' && '⟳ Working…'}
                  {refreshStatus === 'done' && '✓ Updated'}
                  {refreshStatus === 'error' && '✕ Error'}
                </span>
              )}
              <RefreshMenu
                running={refreshRunning}
                selectionMode={selectionMode}
                selectedCount={selected.size}
                defaultCount={filters.limit}
                onSoft={handleSoftRefresh}
                onHardCorpus={handleHardCorpus}
                onHardLive={handleHardLive}
                onToggleSelectionMode={() => setSelectionMode((v) => !v)}
                onRefreshSelected={handleRefreshSelected}
              />
            </>
          )}
        </div>
      </header>

      <CollectionsBar
        collections={collections}
        activeId={activeId}
        songsActive={section === 'songs'}
        onSelectHome={goHome}
        onSelectSongs={goSongs}
        onSelectCollection={openCollection}
        onCreate={(title) => { onCreateCollection(title); }}
      />

      {section === 'digest' && activeId === null && (
        <FilterBar filters={filters} meta={meta} onChange={handleFiltersChange} />
      )}

      {section === 'songs' && (
        <SongsView
          collections={collections}
          onCreateCollection={onCreateCollection}
          reloadCollections={loadCollections}
        />
      )}

      {section === 'digest' && (
      <main className="digest-main">
        {error && (
          <div className="error-banner"><strong>Error:</strong> {error}</div>
        )}

        {loading && <div className="loading">Scanning specimens…</div>}

        {/* ── Collection view ─────────────────────────────────────────── */}
        {!loading && !error && activeCollection && (
          <>
            <div className="collection-head">
              <div>
                <h2 className="collection-title">{activeCollection.title}</h2>
                {activeCollection.description && (
                  <p className="collection-desc">{activeCollection.description}</p>
                )}
                <span className="result-filter">{activeCollection.item_count} saved</span>
              </div>
              <div className="collection-head-actions">
                <button className="btn-ghost" onClick={onRenameCollection}>Rename</button>
                <button className="btn-ghost btn-ghost--danger" onClick={onDeleteCollection}>Delete</button>
              </div>
            </div>
            {activeCollection.cards.length === 0 ? (
              <div className="empty-state">
                No posts in this collection yet.
                <br />
                <span className="empty-hint">Use the 🔖 button on any card to save it here.</span>
              </div>
            ) : (
              <div className="card-grid">
                {activeCollection.cards.map((card, i) => (
                  <DigestCard
                    key={keyOf(card)}
                    card={card}
                    rank={i + 1}
                    onOpen={handleOpenCard}
                    {...cardProps(true)}
                  />
                ))}
              </div>
            )}
          </>
        )}

        {/* ── Home (digest) view ──────────────────────────────────────── */}
        {!loading && !error && !activeCollection && (
          <>
            <div className="result-meta">
              <span className="result-count">{totalCount.toLocaleString()}</span>
              {' specimens'}
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
                    key={keyOf(card)}
                    card={card}
                    rank={i + 1}
                    onOpen={handleOpenCard}
                    selectable={selectionMode}
                    selected={selected.has(keyOf(card))}
                    onToggleSelect={onToggleSelect}
                    {...cardProps(false)}
                  />
                ))}
              </div>
            )}
          </>
        )}
      </main>
      )}

      {openCard && section === 'digest' && (
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
