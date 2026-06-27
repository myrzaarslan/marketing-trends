import type {
  Collection,
  CollectionDetail,
  ContentBundle,
  DigestFilters,
  DigestMeta,
  DigestResponse,
  HardRefreshRequest,
  RefreshStatus,
  ResnapshotResult,
  SnapshotSeries,
  Song,
  SongDetail,
  SongFilters,
  SongHardRefreshRequest,
  SongsResponse,
} from './types';

const API_BASE = import.meta.env.VITE_API_URL ?? '';

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, init);
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`API ${path} → ${res.status}: ${text}`);
  }
  return res.json() as Promise<T>;
}

function jsonInit(method: string, body?: unknown): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  };
}

// --- Digest -----------------------------------------------------------------

export function fetchDigest(filters: DigestFilters): Promise<DigestResponse> {
  const params = new URLSearchParams();
  if (filters.platforms && filters.platforms.length) {
    params.set('platforms', filters.platforms.join(','));
  } else if (filters.platform) {
    params.set('platform', filters.platform);
  }
  if (filters.geo) params.set('geo', filters.geo);
  params.set('period', String(filters.period));
  params.set('sort', filters.sort);
  params.set('limit', String(filters.limit));
  if (filters.unseen_only) params.set('unseen_only', 'true');
  return apiFetch<DigestResponse>(`/digest?${params}`);
}

export function fetchDigestMeta(platform?: string): Promise<DigestMeta> {
  const params = new URLSearchParams();
  if (platform) params.set('platform', platform);
  return apiFetch<DigestMeta>(`/digest/meta?${params}`);
}

export function fetchPost(platform: string, platformPostId: string): Promise<ContentBundle> {
  return apiFetch<ContentBundle>(`/post/${platform}/${platformPostId}`);
}

export function fetchSnapshots(
  platform: string,
  platformPostId: string,
): Promise<SnapshotSeries> {
  return apiFetch<SnapshotSeries>(`/post/${platform}/${platformPostId}/snapshots`);
}

/** Re-observe the post live now, append a fresh snapshot, return the updated series. */
export function resnapshotPost(
  platform: string,
  platformPostId: string,
): Promise<ResnapshotResult> {
  return apiFetch<ResnapshotResult>(
    `/post/${platform}/${platformPostId}/resnapshot`,
    { method: 'POST' },
  );
}

/** Priority-download a single post's media on demand. Returns the fresh bundle. */
export function enrichPost(
  platform: string,
  platformPostId: string,
  force = false,
): Promise<ContentBundle> {
  const q = force ? '?force=true' : '';
  return apiFetch<ContentBundle>(`/post/${platform}/${platformPostId}/enrich${q}`, { method: 'POST' });
}

// --- Songs (viral sounds) ---------------------------------------------------

export function fetchSongs(filters: SongFilters): Promise<SongsResponse> {
  const params = new URLSearchParams();
  if (filters.platform) params.set('platform', filters.platform);
  if (filters.geo) params.set('geo', filters.geo);
  params.set('period', String(filters.period));
  params.set('sort', filters.sort);
  params.set('limit', String(filters.limit));
  if (filters.unseen_only) params.set('unseen_only', 'true');
  return apiFetch<SongsResponse>(`/songs?${params}`);
}

export function fetchSongDetail(
  song: Pick<Song, 'platform' | 'key'>,
  filters: Pick<SongFilters, 'geo' | 'period' | 'sort'>,
): Promise<SongDetail> {
  const params = new URLSearchParams();
  params.set('platform', song.platform);
  params.set('key', song.key);
  if (filters.geo) params.set('geo', filters.geo);
  params.set('period', String(filters.period));
  params.set('sort', filters.sort);
  return apiFetch<SongDetail>(`/song?${params}`);
}

export function putSongFlags(
  platform: string,
  key: string,
  flags: { hidden?: boolean; pinned?: boolean },
): Promise<{ hidden: boolean; pinned: boolean }> {
  return apiFetch('/song/flags', jsonInit('PUT', { platform, key, ...flags }));
}

/** Direct URL to download a song's audio file (served by the API). */
export function songAudioUrl(platform: string, key: string): string {
  const params = new URLSearchParams({ platform, key });
  return `${API_BASE}/song/audio?${params}`;
}

export function triggerSongHardRefresh(
  req: SongHardRefreshRequest,
): Promise<{ job_id: string }> {
  return apiFetch<{ job_id: string }>('/songs/refresh/hard', jsonInit('POST', req));
}

// --- Refresh ----------------------------------------------------------------

export async function triggerRefresh(): Promise<{ job_id: string }> {
  return apiFetch<{ job_id: string }>('/refresh?seed_scratch=true', { method: 'POST' });
}

export function triggerHardRefresh(req: HardRefreshRequest): Promise<{ job_id: string }> {
  return apiFetch<{ job_id: string }>('/refresh/hard', jsonInit('POST', req));
}

export function fetchRefreshStatus(jobId: string): Promise<RefreshStatus> {
  return apiFetch<RefreshStatus>(`/refresh/status/${jobId}`);
}

// --- Collections ------------------------------------------------------------

export function listCollections(): Promise<{ collections: Collection[] }> {
  return apiFetch<{ collections: Collection[] }>('/collections');
}

export function createCollection(title: string, description?: string): Promise<Collection> {
  return apiFetch<Collection>('/collections', jsonInit('POST', { title, description }));
}

export function updateCollection(
  id: number,
  patch: { title?: string; description?: string },
): Promise<Collection> {
  return apiFetch<Collection>(`/collections/${id}`, jsonInit('PATCH', patch));
}

export function deleteCollection(id: number): Promise<{ deleted: number }> {
  return apiFetch<{ deleted: number }>(`/collections/${id}`, { method: 'DELETE' });
}

export function getCollection(id: number): Promise<CollectionDetail> {
  return apiFetch<CollectionDetail>(`/collections/${id}`);
}

export function addToCollection(
  id: number,
  platform: string,
  platformPostId: string,
): Promise<{ added: boolean; collection_id: number }> {
  return apiFetch(`/collections/${id}/items`, jsonInit('POST', {
    platform,
    platform_post_id: platformPostId,
  }));
}

export function removeFromCollection(
  id: number,
  platform: string,
  platformPostId: string,
): Promise<{ removed: boolean }> {
  return apiFetch(`/collections/${id}/items/${platform}/${platformPostId}`, { method: 'DELETE' });
}

// --- Notes ------------------------------------------------------------------

export function putNote(
  platform: string,
  platformPostId: string,
  body: string,
): Promise<{ note: string | null }> {
  return apiFetch(`/post/${platform}/${platformPostId}/note`, jsonInit('PUT', { body }));
}

export function deleteNote(
  platform: string,
  platformPostId: string,
): Promise<{ note: null }> {
  return apiFetch(`/post/${platform}/${platformPostId}/note`, { method: 'DELETE' });
}

// --- Flags (hide / pin) -----------------------------------------------------

export function putFlags(
  platform: string,
  platformPostId: string,
  flags: { hidden?: boolean; pinned?: boolean },
): Promise<{ hidden: boolean; pinned: boolean }> {
  return apiFetch(`/post/${platform}/${platformPostId}/flags`, jsonInit('PUT', flags));
}
