import { LitElement, html, css, svg, nothing } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import type { HomeAssistant, LovelaceCardConfig } from "custom-card-helpers";
import { formatTime } from "custom-card-helpers";
import { localize } from "./localize/localize";
import { formatDuration } from "./format";

/**
 * mobius-schedule-card
 *
 * One card per schedule GROUP -- not per tank, and not per light/pump
 * device in isolation. Config is a single field, device_id, and it
 * must be a MEMBER's own device_id (a specific light or pump) -- never
 * the Tank device itself, even though the backend call this card
 * actually needs (mobius/resolve_schedule_groups) takes the Tank's own
 * ID. This card resolves that itself: given the configured device_id,
 * it looks up that device's own via_device_id (every light/pump this
 * integration creates is registered with via_device_id pointing at its
 * own Tank -- see __init__.py/sensor.py/switch.py/button.py/select.py,
 * all of which set this identically) to find its Tank, calls
 * resolve_schedule_groups with THAT, then shows only the one group
 * containing the originally configured device -- so a tank with two
 * independently-grouped light pairs needs two separate card instances,
 * one per group, each pointed at any one of that group's own members.
 */

interface MobiusScheduleCardConfig extends LovelaceCardConfig {
  device_id: string;
}

function joinNaturally(names: string[]): string {
  if (names.length <= 1) return names[0] || "";
  if (names.length === 2) return `${names[0]} and ${names[1]}`;
  return `${names.slice(0, -1).join(", ")}, and ${names[names.length - 1]}`;
}

const CHART_WIDTH = 600;
const CHART_HEIGHT = 140;

// Best-effort visual match to a channel's own real-world color, for
// the handful of channel names the app itself uses across its
// current fixture lineup -- purely cosmetic (channel identity itself
// comes entirely from the name/entity_id, never from this mapping).
// Anything unmatched (including a channel name unique to a fixture
// this map hasn't seen) cycles through a small fixed palette by
// position, so it's still visually distinguishable from its neighbors
// without pretending to know what color it actually corresponds to.
const CHANNEL_COLOR_GUESSES: Record<string, string> = {
  royalblue: "#4169e1",
  blue: "#2f6fed",
  violet: "#8a2be2",
  uv: "#9400d3",
  white: "#e0e0e0",
  red: "#e53935",
  green: "#43a047",
  "deep red": "#b71c1c",
};
const FALLBACK_PALETTE = ["#4fc3f7", "#ff8a65", "#aed581", "#ba68c8", "#ffd54f"];

function channelColor(name: string): string {
  const guess = CHANNEL_COLOR_GUESSES[name.toLowerCase()];
  if (guess) return guess;
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = (hash * 31 + name.charCodeAt(i)) >>> 0;
  return FALLBACK_PALETTE[hash % FALLBACK_PALETTE.length];
}

// custom-card-helpers' own HomeAssistant type is a deliberately
// minimal subset (states/services/config/etc.) -- it does NOT include
// the device/entity registries, even though the real hass object
// passed to every card at runtime genuinely has them. Extending
// locally (rather than casting to `any` every time this is needed)
// keeps everything past this one boundary still type-checked.
interface DeviceRegistryEntry {
  id: string;
  via_device_id: string | null;
}
interface ExtendedHomeAssistant extends HomeAssistant {
  devices: Record<string, DeviceRegistryEntry>;
}

interface ScheduleGroupMember {
  device_id: string;
  serial: string;
  name: string;
  channel_entity_ids?: Record<string, string | null>;
  schedule_intensity_entity_id?: string | null;
  speed_entity_id?: string | null;
  flow_entity_id?: string | null;
  mode_entity_id?: string | null;
}

interface ScheduleGroup {
  kind: "light" | "pump";
  group_mask: number | null;
  members: ScheduleGroupMember[];
  channels?: string[];
  modes?: string[];
  active_scene: { name: string; duration_seconds: number } | null;
  scene_entity_id: string | null;
  schedule_intensity?: number | null;
}

// Home Assistant's own "compressed state" wire format, used by the
// history/history_during_period websocket command -- s/lu/lc rather
// than state/last_updated/last_changed. lu is itself optional: HA
// omits it when it's identical to lc, so the real timestamp to use is
// whichever of the two is actually present.
interface CompressedStateEntry {
  s: string;
  lu?: number;
  lc?: number;
}

interface HistoryPoint {
  t: number; // ms since epoch
  v: number;
}

@customElement("mobius-schedule-card")
export class MobiusScheduleCard extends LitElement {
  @property({ attribute: false }) public hass!: ExtendedHomeAssistant;

  @state() private _config?: MobiusScheduleCardConfig;
  @state() private _group?: ScheduleGroup;
  @state() private _error?: string;
  @state() private _loading = false;

  // Shown instead of the live entity value while a person is actively
  // dragging the intensity slider -- the debounced service call below
  // hasn't necessarily landed yet, so reading the live entity value
  // during that window would make the slider visibly snap back before
  // jumping to the new value once the service call actually completes.
  // Cleared once the debounced write itself resolves, so the display
  // reverts to tracking the live entity normally again afterward.
  @state() private _pendingIntensityPercent?: number;

  @state() private _channelHistoryByEntity: Record<string, HistoryPoint[]> = {};
  @state() private _historyLoading = false;

  // Not @state -- this is a plain timer handle, not something that
  // should itself trigger a re-render when it changes.
  private _intensityDebounceHandle?: ReturnType<typeof setTimeout>;

  // The device_id resolution was last run for -- avoids re-resolving
  // on every single hass update (which happens on every poll cycle
  // for ANY entity in the whole system, not just this card's own
  // ones); only actually re-runs when the configured device_id itself
  // changes, or after a failed attempt where hass wasn't ready yet.
  private _resolvedFor?: string;

  public setConfig(config: MobiusScheduleCardConfig): void {
    if (!config || !config.device_id) {
      throw new Error('mobius-schedule-card: "device_id" is required (a light or pump device, never the Tank itself)');
    }
    this._config = config;
    this._resolvedFor = undefined;
    this._group = undefined;
    this._error = undefined;
  }

  public getCardSize(): number {
    return this._group?.kind === "light" ? 6 : 4;
  }

  public getGridOptions() {
    return { rows: 6, columns: 6, min_rows: 4 };
  }

  public static getStubConfig(): Partial<MobiusScheduleCardConfig> {
    return { device_id: "" };
  }

  public static getConfigForm() {
    return {
      schema: [{ name: "device_id", required: true, selector: { device: { integration: "mobius" } } }] as const,
      computeLabel: (schema: { name: string }) =>
        schema.name === "device_id" ? localize("editor.device_id") : undefined,
      computeHelper: (schema: { name: string }) =>
        schema.name === "device_id" ? localize("editor.device_id_helper") : undefined,
    };
  }

  protected willUpdate(): void {
    if (!this.hass || !this._config) return;
    if (this._resolvedFor === this._config.device_id) return;
    this._resolveGroup();
  }

  private async _resolveGroup(): Promise<void> {
    const deviceId = this._config!.device_id;
    this._resolvedFor = deviceId;
    this._loading = true;
    this._error = undefined;

    try {
      const device = this.hass.devices?.[deviceId];
      if (!device) {
        throw new Error(localize("schedule_card.device_not_found"));
      }
      const tankDeviceId = device.via_device_id;
      if (!tankDeviceId) {
        throw new Error(localize("schedule_card.no_tank_found"));
      }

      const response = await this.hass.callWS<{ groups: ScheduleGroup[] }>({
        type: "mobius/resolve_schedule_groups",
        device_id: tankDeviceId,
      });

      const group = response.groups.find((g) => g.members.some((m) => m.device_id === deviceId));
      if (!group) {
        throw new Error(localize("schedule_card.group_not_found"));
      }
      this._group = group;

      if (group.kind === "light") {
        // Deliberately not awaited -- the glance view itself doesn't
        // depend on history to render (readings/slider/scene banner
        // all come from live state), so a slow history query
        // shouldn't hold up everything else. The chart area just
        // shows its own loading state until this resolves.
        this._fetchChannelHistory(group);
      }
    } catch (err) {
      this._error = err instanceof Error ? err.message : String(err);
      this._group = undefined;
    } finally {
      this._loading = false;
    }
  }

  // One entity_id -> point[] map covering EVERY member's own channel
  // sensors, fetched once per group resolution -- not per currently-
  // picked fallback member. This means a fallback switch (a member
  // recovering or going unavailable) can redraw the chart instantly
  // from already-fetched data, with no second network round-trip, by
  // simply picking a different member's own entries out of this same
  // map at render time (see _pickAvailableLightMember, used
  // identically for both the live reading and the chart).
  private async _fetchChannelHistory(group: ScheduleGroup): Promise<void> {
    const entityIds = group.members
      .flatMap((m) => Object.values(m.channel_entity_ids ?? {}))
      .filter((id): id is string => !!id);
    if (entityIds.length === 0) return;

    this._historyLoading = true;
    try {
      const startOfDay = new Date();
      startOfDay.setHours(0, 0, 0, 0);

      const response = await this.hass.callWS<Record<string, CompressedStateEntry[]>>({
        type: "history/history_during_period",
        start_time: startOfDay.toISOString(),
        entity_ids: entityIds,
        no_attributes: true,
        minimal_response: true,
      });

      const byEntity: Record<string, HistoryPoint[]> = {};
      for (const [entityId, entries] of Object.entries(response)) {
        byEntity[entityId] = entries
          .map((e) => ({ t: (e.lu ?? e.lc ?? 0) * 1000, v: Number(e.s) }))
          .filter((p) => Number.isFinite(p.v));
      }
      this._channelHistoryByEntity = byEntity;
    } catch {
      // The glance view itself still works without history -- the
      // chart area just shows its own "couldn't load" state instead
      // of failing the whole card the way a resolution error does.
      this._channelHistoryByEntity = {};
    } finally {
      this._historyLoading = false;
    }
  }

  // Reads live, not the group's own active_scene snapshot (only
  // current as of whenever resolve_schedule_groups last ran) --
  // scene_entity_id is tank-wide structural metadata that rarely
  // changes, but which scene is active and how long it has left
  // change constantly, and hass updates reactively on every real
  // entity state change, unlike a resolve_schedule_groups snapshot.
  private _renderSceneBanner() {
    const entityId = this._group?.scene_entity_id;
    if (!entityId) return nothing;
    const stateObj = this.hass.states[entityId];
    if (!stateObj || stateObj.state === "None" || stateObj.state === "unavailable") return nothing;

    const duration = stateObj.attributes.duration_remaining_seconds as number | undefined;
    return html`
      <div class="scene-banner">
        <ha-icon icon="mdi:auto-mode"></ha-icon>
        <span>
          <strong>${stateObj.state}</strong> ${localize("schedule_card.scene_running_instead")}
          ${duration != null ? html`(${formatDuration(duration)} ${localize("schedule_card.remaining_suffix")})` : nothing}
        </span>
      </div>
    `;
  }

  private _renderPumpGlance() {
    const group = this._group!;
    const member = group.members[0];
    const flowState = member.flow_entity_id ? this.hass.states[member.flow_entity_id] : undefined;
    const speedState = member.speed_entity_id ? this.hass.states[member.speed_entity_id] : undefined;
    const modeState = member.mode_entity_id ? this.hass.states[member.mode_entity_id] : undefined;

    const flowAvailable = flowState && flowState.state !== "unavailable" && flowState.state !== "unknown";
    const speedAvailable = speedState && speedState.state !== "unavailable" && speedState.state !== "unknown";

    return html`
      <ha-card>
        <div class="header">
          <div class="title">${localize("schedule_card.pump_title")}</div>
          <div class="subtitle">${member.name}</div>
        </div>
        ${this._renderSceneBanner()}
        ${
          flowAvailable
            ? html`
                <div class="reading">
                  <span class="reading-value">${flowState!.state}</span>
                  <!-- Never a hardcoded assumption (e.g. "L/h") -- the
                  device itself always reports GPH, but Home Assistant's
                  own per-entity unit override (available for
                  volume_flow_rate-class sensors) converts BOTH state
                  and unit_of_measurement server-side, before this card
                  ever sees them. Always displaying whatever this
                  attribute actually says is what makes a person's own
                  configured unit (GPH, L/h, or anything else) show up
                  correctly, with zero unit-specific logic needed here
                  at all. -->
                  <span class="reading-unit">${flowState!.attributes.unit_of_measurement}</span>
                </div>
              `
            : speedAvailable
              ? html`
                  <div class="reading">
                    <span class="reading-value">${speedState!.state}%</span>
                    <span class="reading-unit">${localize("schedule_card.speed_not_reliable")}</span>
                  </div>
                `
              : html`<div class="reading-missing">${localize("schedule_card.no_flow_data")}</div>`
        }
        ${
          modeState
            ? html`
                <div class="current-mode">
                  ${localize("schedule_card.currently_running")} <strong>${modeState.state}</strong>
                </div>
              `
            : nothing
        }
        <button class="edit-button">${localize("schedule_card.edit_schedule")}</button>
      </ha-card>
    `;
  }

  // Try each member IN ORDER (already sorted, deterministic --
  // resolve_schedule_groups sorts by serial) for one that's currently
  // available, stopping once found. A single bounded pass through a
  // fixed list, never retried or re-entered, so "every member
  // unavailable at once" falls through to the null case below instead
  // of looping. "Available" is judged from one representative channel
  // sensor per member (the first channel in this group's own list) --
  // every channel sensor on the same physical device transitions
  // together (they're all polled from the same coordinator), so
  // checking one is exactly as informative as checking all of them.
  private _pickAvailableLightMember(): { member?: ScheduleGroupMember; unavailableNames: string[] } {
    const group = this._group!;
    const unavailableNames: string[] = [];
    let picked: ScheduleGroupMember | undefined;

    for (const member of group.members) {
      const entityIds = member.channel_entity_ids ? Object.values(member.channel_entity_ids) : [];
      const representativeId = entityIds.find((id) => id != null);
      const stateObj = representativeId ? this.hass.states[representativeId] : undefined;
      const available = !!stateObj && stateObj.state !== "unavailable" && stateObj.state !== "unknown";

      if (available) {
        if (!picked) picked = member;
      } else {
        unavailableNames.push(member.name);
      }
    }

    return { member: picked, unavailableNames };
  }

  private _handleIntensityChange(percent: number, deviceId: string): void {
    this._pendingIntensityPercent = percent;
    clearTimeout(this._intensityDebounceHandle);
    this._intensityDebounceHandle = setTimeout(() => {
      this.hass
        .callService("mobius", "set_schedule_intensity", { device_id: deviceId, intensity: percent })
        .finally(() => {
          this._pendingIntensityPercent = undefined;
        });
    }, 1200);
  }

  private _renderChannelChart(sourceMember: ScheduleGroupMember) {
    if (this._historyLoading) {
      return html`<div class="chart-status">${localize("schedule_card.loading_history")}</div>`;
    }

    const entries = Object.entries(sourceMember.channel_entity_ids ?? {}).filter(
      (entry): entry is [string, string] => !!entry[1],
    );
    if (entries.length === 0) {
      return html`<div class="chart-status">${localize("schedule_card.no_channel_data")}</div>`;
    }

    const startOfDay = new Date();
    startOfDay.setHours(0, 0, 0, 0);
    const dayStartMs = startOfDay.getTime();
    const dayMs = 24 * 60 * 60 * 1000;

    const toX = (t: number) => ((t - dayStartMs) / dayMs) * CHART_WIDTH;
    const toY = (v: number) => CHART_HEIGHT - (Math.max(0, Math.min(100, v)) / 100) * CHART_HEIGHT;

    const lines = entries
      .map(([channelName, entityId]) => {
        const points = this._channelHistoryByEntity[entityId];
        if (!points || points.length === 0) return nothing;
        const path = points.map((p) => `${toX(p.t).toFixed(1)},${toY(p.v).toFixed(1)}`).join(" ");
        return svg`<polyline points=${path} fill="none" stroke=${channelColor(channelName)} stroke-width="2" />`;
      })
      .filter((l) => l !== nothing);

    if (lines.length === 0) {
      return html`<div class="chart-status">${localize("schedule_card.no_history_yet")}</div>`;
    }

    const nowX = toX(Date.now());
    const lang = this.hass?.locale;
    const hourLabel = (hour: number) => {
      const d = new Date(startOfDay);
      d.setHours(hour);
      return lang ? formatTime(d, lang) : `${hour}:00`;
    };

    return html`
      <svg class="chart" viewBox="0 0 ${CHART_WIDTH} ${CHART_HEIGHT + 16}" preserveAspectRatio="none">
        ${[0, 6, 12, 18].map(
          (hour) => svg`
            <line
              x1=${toX(dayStartMs + hour * 3600000)}
              x2=${toX(dayStartMs + hour * 3600000)}
              y1="0"
              y2=${CHART_HEIGHT}
              class="chart-gridline"
            />
            <text x=${toX(dayStartMs + hour * 3600000)} y=${CHART_HEIGHT + 12} class="chart-label">
              ${hourLabel(hour)}
            </text>
          `,
        )}
        <line x1=${nowX} x2=${nowX} y1="0" y2=${CHART_HEIGHT} class="chart-now-line" />
        ${lines}
      </svg>
    `;
  }

  private _renderLightGlance() {
    const group = this._group!;
    const { member: sourceMember, unavailableNames } = this._pickAvailableLightMember();

    const intensityEntityId = sourceMember?.schedule_intensity_entity_id;
    const intensityState = intensityEntityId ? this.hass.states[intensityEntityId] : undefined;
    const liveIntensityPercent = intensityState ? Math.round(Number(intensityState.state)) : undefined;
    const displayedIntensityPercent = this._pendingIntensityPercent ?? liveIntensityPercent;

    return html`
      <ha-card>
        <div class="header">
          <div class="title">${localize("schedule_card.light_title")}</div>
          <div class="subtitle">${group.members.map((m) => m.name).join(" + ")}</div>
        </div>
        ${this._renderSceneBanner()}
        ${
          unavailableNames.length > 0
            ? html`
                <div class="unavailable-note">
                  ${joinNaturally(unavailableNames)}
                  ${
                    unavailableNames.length === 1
                      ? localize("schedule_card.is_unavailable")
                      : localize("schedule_card.are_unavailable")
                  }
                </div>
              `
            : nothing
        }
        ${
          sourceMember
            ? this._renderChannelChart(sourceMember)
            : html`<div class="reading-missing">${localize("schedule_card.no_channel_data")}</div>`
        }
        ${
          displayedIntensityPercent != null
            ? html`
                <div class="intensity-row">
                  <div class="intensity-label">
                    <span>${localize("schedule_card.overall_intensity")}</span>
                    <span>${displayedIntensityPercent}%</span>
                  </div>
                  <input
                    type="range"
                    min="0"
                    max="100"
                    .value=${String(displayedIntensityPercent)}
                    @input=${(e: Event) =>
                      this._handleIntensityChange(
                        Number((e.target as HTMLInputElement).value),
                        this._config!.device_id,
                      )}
                  />
                </div>
              `
            : nothing
        }
        <button class="edit-button">${localize("schedule_card.edit_schedule")}</button>
      </ha-card>
    `;
  }

  protected render() {
    if (!this._config) return nothing;

    if (this._error) {
      return html`
        <ha-card>
          <div class="warning">${this._error}</div>
        </ha-card>
      `;
    }

    if (this._loading || !this._group) {
      return html`
        <ha-card>
          <div class="loading">${localize("schedule_card.loading")}</div>
        </ha-card>
      `;
    }

    if (this._group.kind === "pump") {
      return this._renderPumpGlance();
    }

    // The full point editor for both kinds lands in follow-up work.
    return this._renderLightGlance();
  }

  static styles = css`
    ha-card {
      padding: 16px;
    }
    .header {
      margin-bottom: 4px;
    }
    .title {
      font-size: 1.2em;
      font-weight: 500;
      color: var(--primary-text-color);
    }
    .subtitle {
      font-size: 0.9em;
      color: var(--secondary-text-color);
      margin-bottom: 8px;
    }
    .warning {
      padding: 12px 0;
      color: var(--error-color, #db4437);
    }
    .loading {
      padding: 12px 0;
      color: var(--secondary-text-color);
    }
    .scene-banner {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 10px;
      border-radius: 8px;
      background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.08);
      font-size: 0.85em;
      color: var(--primary-text-color);
      margin-bottom: 12px;
    }
    .scene-banner ha-icon {
      color: var(--primary-color);
      --mdc-icon-size: 18px;
      flex-shrink: 0;
    }
    .reading {
      display: flex;
      align-items: baseline;
      gap: 8px;
      margin: 4px 0 6px 0;
    }
    .reading-value {
      font-size: 2.2em;
      font-weight: 300;
      color: var(--primary-text-color);
    }
    .reading-unit {
      font-size: 0.85em;
      color: var(--secondary-text-color);
    }
    .reading-missing {
      padding: 12px 0;
      color: var(--secondary-text-color);
      font-size: 0.9em;
    }
    .current-mode {
      font-size: 0.85em;
      color: var(--secondary-text-color);
      margin-bottom: 14px;
    }
    .edit-button {
      width: 100%;
      background: var(--secondary-background-color, rgba(0, 0, 0, 0.05));
      border: 1px solid var(--divider-color);
      border-radius: 10px;
      padding: 10px 0;
      color: var(--primary-text-color);
      font-family: inherit;
      font-size: 0.9em;
      font-weight: 500;
      cursor: pointer;
    }
    .edit-button:hover {
      border-color: var(--primary-color);
    }
    .unavailable-note {
      font-size: 0.8em;
      color: var(--secondary-text-color);
      margin-bottom: 8px;
    }
    .intensity-row {
      margin-bottom: 14px;
    }
    .intensity-label {
      display: flex;
      justify-content: space-between;
      font-size: 0.85em;
      color: var(--secondary-text-color);
      margin-bottom: 4px;
    }
    .intensity-row input[type="range"] {
      width: 100%;
      accent-color: var(--primary-color);
    }
    .chart {
      width: 100%;
      height: auto;
      margin-bottom: 12px;
    }
    .chart-gridline {
      stroke: var(--divider-color);
      stroke-width: 1;
    }
    .chart-now-line {
      stroke: var(--primary-color);
      stroke-width: 1;
      stroke-dasharray: 3, 3;
    }
    .chart-label {
      font-size: 9px;
      fill: var(--secondary-text-color);
      text-anchor: middle;
    }
    .chart-status {
      padding: 24px 0;
      text-align: center;
      color: var(--secondary-text-color);
      font-size: 0.85em;
    }
  `;
}

declare global {
  interface HTMLElementTagNameMap {
    "mobius-schedule-card": MobiusScheduleCard;
  }
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "mobius-schedule-card",
  name: "Mobius Schedule",
  description: "View and edit a Mobius light or pump schedule.",
  preview: false,
});
