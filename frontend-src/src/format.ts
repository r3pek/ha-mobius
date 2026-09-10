/** m:ss, e.g. 125 -> "2:05". Shared by both cards for a scene's own
 * remaining-time display. */
export function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}
