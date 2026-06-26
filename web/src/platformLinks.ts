// Profile-URL builders. Unlike post permalinks (which need a platform shortcode
// we don't always have), a profile URL only needs the account handle — which we
// always capture — so this is the robust way to link back to the source.

export function profileUrl(platform: string, handle: string | null | undefined): string | null {
  if (!handle) return null;
  const h = handle.replace(/^@+/, '').trim();
  if (!h) return null;
  switch (platform) {
    case 'tiktok':
      return `https://www.tiktok.com/@${h}`;
    case 'instagram':
      return `https://www.instagram.com/${h}/`;
    case 'threads':
      return `https://www.threads.com/@${h}`;
    case 'x':
      return `https://x.com/${h}`;
    default:
      return null;
  }
}
