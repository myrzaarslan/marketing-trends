export type SortKey =
  | 'raw_counts'
  | 'engagement_rate'
  | 'engagement_rate_followers'
  | 'share_rate'
  | 'save_rate'
  | 'velocity'
  | 'relative_baseline'
  | 'cross_persona';

export type Platform = 'tiktok' | 'instagram' | 'x' | 'threads';
export type GeoTier = 'KZ' | 'CIS' | 'World';

export interface SortAvailability {
  raw_counts: boolean;
  engagement_rate: boolean;
  engagement_rate_followers: boolean;
  share_rate: boolean;
  save_rate: boolean;
  velocity: boolean;
  relative_baseline: boolean;
  cross_persona: boolean;
}

export interface DigestCard {
  platform: string;
  platform_post_id: string;
  account_handle: string;
  url: string;
  caption: string | null;
  hashtags: string[];
  sound_id: string | null;
  sound_name: string | null;
  media_type: string;
  geo_tier: string | null;
  thumbnail_path: string | null;
  /** Resolved thumbnail URL from API (preferred over thumbnail_path) */
  thumbnail: string | null;
  posted_at: string | null;
  first_seen_at: string;
  last_seen_at: string;
  view_count: number | null;
  like_count: number | null;
  comment_count: number | null;
  share_count: number | null;
  save_count: number | null;
  author_follower_count: number | null;
  score: number | null;
  sort_used: SortKey;
  sort_requested: SortKey;
  snapshot_days: number;
  snapshot_count?: number;
  account_post_count?: number;
  distinct_source_count?: number;
  has_history: boolean;
  /** Legacy — use has_content_bundle */
  has_content: boolean;
  /** True if a full Content Bundle (media + caption + sound) exists */
  has_content_bundle: boolean;
  sort_availability: SortAvailability;
}

export interface DigestResponse {
  count: number;
  platform: string | null;
  geo_tier: string | null;
  period_days: number;
  sort: string;
  cards: DigestCard[];
}

export interface DigestMeta {
  platform: string | null;
  has_history: boolean;
  sort_availability: SortAvailability;
  history_gate_days: number;
  default_sort: SortKey;
}

export interface RefreshStatus {
  job_id: string;
  status: 'queued' | 'running' | 'done' | 'error';
  started_at: number | null;
  finished_at: number | null;
  summary: Record<string, unknown> | null;
  error: string | null;
}

export interface DigestFilters {
  platform: string;
  geo: string;
  period: number;
  sort: SortKey;
  limit: number;
}

// ---------------------------------------------------------------------------
// Content Bundle
// ---------------------------------------------------------------------------

export interface MediaItem {
  url: string;
  filename: string;
  kind: 'video' | 'image' | 'audio' | 'unknown';
}

export interface ContentBundle {
  platform: string;
  platform_post_id: string;
  enriched: boolean;

  media_type: string;
  media_items: MediaItem[];
  thumbnail: string | null;

  caption: string | null;
  hashtags: string[];
  has_spoiler: boolean;
  spoiler_text: string | null;

  sound_id: string | null;
  sound_name: string | null;
  sound_author: string | null;

  author_display_name: string | null;
  account_handle: string;

  view_count: number | null;
  like_count: number | null;
  comment_count: number | null;
  share_count: number | null;
  save_count: number | null;
  author_follower_count: number | null;

  url: string;
  geo_tier: string | null;
  posted_at: string | null;
  first_seen_at: string;

  rank: number | null;
  score: number | null;
  sort_used: string | null;
}
