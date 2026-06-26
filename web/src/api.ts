import type { ContentBundle, DigestFilters, DigestMeta, DigestResponse, RefreshStatus } from './types';

const API_BASE = import.meta.env.VITE_API_URL ?? '';

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, init);
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`API ${path} → ${res.status}: ${text}`);
  }
  return res.json() as Promise<T>;
}

export function fetchDigest(filters: DigestFilters): Promise<DigestResponse> {
  const params = new URLSearchParams();
  if (filters.platform) params.set('platform', filters.platform);
  if (filters.geo) params.set('geo', filters.geo);
  params.set('period', String(filters.period));
  params.set('sort', filters.sort);
  params.set('limit', String(filters.limit));
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

export async function triggerRefresh(): Promise<{ job_id: string }> {
  return apiFetch<{ job_id: string }>('/refresh?seed_scratch=true', { method: 'POST' });
}

export function fetchRefreshStatus(jobId: string): Promise<RefreshStatus> {
  return apiFetch<RefreshStatus>(`/refresh/status/${jobId}`);
}
