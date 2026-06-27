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

  // User state (collections / notes / refresh)
  note?: string | null;
  hidden?: boolean;
  pinned?: boolean;
  collection_ids?: number[];
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
  /** Multi-platform subset (e.g. after a multi-platform refresh); overrides `platform`. */
  platforms?: string[];
  geo: string;
  period: number;
  sort: SortKey;
  limit: number;
  /** When true, only show never-served posts (+ pinned) — the hard-refresh working set */
  unseen_only?: boolean;
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

  // User state
  note?: string | null;
  hidden?: boolean;
  pinned?: boolean;
  collection_ids?: number[];
}

// ---------------------------------------------------------------------------
// Snapshot time series (stats graph)
// ---------------------------------------------------------------------------

export interface SnapshotPoint {
  fetched_at: string;
  view_count: number | null;
  like_count: number | null;
  comment_count: number | null;
  share_count: number | null;
  save_count: number | null;
  author_follower_count: number | null;
  source: string | null;
}

export interface SnapshotSeries {
  platform: string;
  platform_post_id: string;
  points: SnapshotPoint[];
  velocity_per_hour: number | null;
  velocity_metric: 'views' | 'likes' | null;
}

export interface ResnapshotResult {
  status: 'updated' | 'not_found' | 'error';
  error: string | null;
  fetched: number;
  series: SnapshotSeries;
}

// ---------------------------------------------------------------------------
// Collections
// ---------------------------------------------------------------------------

export interface Collection {
  id: number;
  title: string;
  description: string | null;
  item_count: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface CollectionDetail extends Collection {
  cards: DigestCard[];
}

// ---------------------------------------------------------------------------
// Refresh
// ---------------------------------------------------------------------------

export type RefreshSource = 'corpus' | 'live';

export interface HardRefreshRequest {
  source: RefreshSource;
  serve_ids: [string, string][];
  platform?: string | null;
  /** Multi-platform selection; overrides `platform`. Omitted/empty = all. */
  platforms?: string[] | null;
  geo?: string | null;
  period?: number;
  sort?: SortKey;
  limit?: number;
  live_per_platform?: number;
}

// ---------------------------------------------------------------------------
// Songs (viral sounds) — TikTok + Instagram only
// ---------------------------------------------------------------------------

export type SongSortKey =
  | 'reuse_count'
  | 'post_count'
  | 'total_views'
  | 'total_engagement'
  | 'avg_engagement_rate'
  | 'rising';

export type SongPlatform = 'tiktok' | 'instagram';

export interface Song {
  platform: SongPlatform;
  key: string;
  sound_id: string | null;
  sound_name: string | null;
  sound_author: string | null;
  top_platform_post_id: string | null;
  geo_tier: string | null;
  latest_first_seen: string | null;
  /** Resolved cover thumbnail URL (from the song's strongest post). */
  thumbnail: string | null;

  /** Cover art from the authoritative Sound row (sound pivot), if any. */
  cover_url?: string | null;
  is_original?: boolean | null;
  /** True when we have an audio source for the sound (pivoted) — show download. */
  downloadable?: boolean;

  // Metrics (all computed; the UI picks which to rank by)
  /** Platform's own count of videos using this sound (TikTok videoCount / IG
   *  formatted clips count) when pivoted, else our observed post_count. */
  reuse_count: number;
  /** 'platform' = authoritative pivot count, 'observed' = our post-count floor. */
  reuse_count_source: 'platform' | 'observed';
  platform_video_count: number | null;
  post_count: number;
  distinct_accounts: number;
  total_views: number;
  total_volume: number;
  total_engagement: number;
  avg_engagement_rate: number | null;
  rising: number;
  recent_post_count: number;

  /** Value of the selected sort, for display. */
  score: number | null;
  sort_used: SongSortKey;

  // User state
  hidden?: boolean;
  pinned?: boolean;
}

export interface SongsResponse {
  count: number;
  platform: SongPlatform | null;
  geo_tier: string | null;
  period_days: number;
  sort: SongSortKey;
  unseen_only: boolean;
  all_sorts: SongSortKey[];
  default_sort: SongSortKey;
  songs: Song[];
}

export interface SongDetail {
  song: Song;
  cards: DigestCard[];
}

export interface SongFilters {
  platform: '' | SongPlatform;
  geo: string;
  period: number;
  sort: SongSortKey;
  limit: number;
  unseen_only?: boolean;
}

export interface SongHardRefreshRequest {
  source: RefreshSource;
  serve_keys: [string, string][];
  platform?: string | null;
  geo?: string | null;
  period?: number;
  sort?: SongSortKey;
  limit?: number;
  live_per_platform?: number;
}
