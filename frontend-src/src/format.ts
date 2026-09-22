/** m:ss, e.g. 125 -> "2:05" (scene remaining time). */
export function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/**
 * Remaining seconds of the running scene from the scene select's
 * attributes: counted down from ends_at (set at each poll) when present,
 * else duration_remaining_seconds as of the last poll.
 */
export function sceneRemainingSeconds(
  attributes: Record<string, unknown>,
  nowMs: number = Date.now(),
): number | undefined {
  const endsAt = typeof attributes.ends_at === "string" ? Date.parse(attributes.ends_at) : NaN;
  if (!Number.isNaN(endsAt)) {
    return Math.max(0, Math.round((endsAt - nowMs) / 1000));
  }
  const duration = attributes.duration_remaining_seconds;
  return typeof duration === "number" ? duration : undefined;
}

/** Re-renders `host` every second while `active` is true. */
export class CountdownTicker {
  private _handle?: ReturnType<typeof setInterval>;

  constructor(private readonly _host: { requestUpdate(): void }) {}

  sync(active: boolean): void {
    if (active && this._handle === undefined) {
      this._handle = setInterval(() => this._host.requestUpdate(), 1000);
    } else if (!active) {
      this.stop();
    }
  }

  stop(): void {
    if (this._handle !== undefined) {
      clearInterval(this._handle);
      this._handle = undefined;
    }
  }
}
