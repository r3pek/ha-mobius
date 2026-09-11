import { LitElement, html, css, svg, nothing, type TemplateResult } from "lit";
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

// Best-effort visual match to a channel's own real-world color --
// purely cosmetic (channel identity itself comes entirely from the
// name/entity_id, never from this mapping). Covers every real
// channel VisualID defines (see below), so the fallback palette
// exists only for a name this map has genuinely never seen (a
// renamed or custom channel), not for any real, currently-defined
// channel -- it cycles through a small fixed palette by position, so
// it's still visually distinguishable from its neighbors without
// pretending to know what color it actually corresponds to.
// Keyed by the exact channel name as python-mobius's own VisualID
// enum reports it (IntEnum.name -- e.g. "DeepRed", "MoonlightBlue":
// PascalCase, no spaces), lowercased to match channelColor()'s own
// lookup. Covers every real light-color channel VisualID defines
// (constants.py) -- excluding Brightness (not a color channel, the
// separate per-point master dimmer) and the Status*/StormProbability/
// CloudProbability entries (not lighting channels at all).
const CHANNEL_COLOR_GUESSES: Record<string, string> = {
  coolwhite: "#d6ecff",
  blue: "#2f6fed",
  royalblue: "#4169e1",
  green: "#43a047",
  red: "#e53935",
  uv: "#9400d3",
  warmwhite: "#ffe0b2",
  violet: "#8a2be2",
  deepblue: "#1a237e",
  deepred: "#b71c1c",
  neutralwhite: "#f5f5dc",
  yellow: "#fdd835",
  amber: "#ffb300",
  farred: "#4a0000",
  // Moonlight channels are conceptually distinct from their daytime
  // namesakes (a dim, cool night-cycle glow, not a bright display
  // color) -- given their own dedicated, visually-distinguishable
  // shades rather than reusing Blue/White's own colors or falling
  // through to the generic hash-based fallback palette below.
  moonlight: "#7986cb",
  moonlightwhite: "#c5cae9",
  moonlightblue: "#5c6bc0",
  cyan: "#00bcd4",
  lime: "#c0ca33",
  blueandwhite: "#90a4ae",
  redandwhite: "#ef9a9a",
  uv_plus: "#7b1fa2",
  white: "#e0e0e0",
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
  mode_params?: Record<string, string[]>;
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

// Matches pump_schedule_to_dict()'s own output shape exactly (python-mobius).
interface PumpScheduleEntry {
  time_minutes: number;
  flags: number;
  mode: string;
  params: Record<string, unknown>;
}

// Matches light_schedule_to_dict()'s own output shape exactly (python-mobius).
interface LightScheduleEntry {
  time_minutes: number;
  flags: number;
  channels: Record<string, number>;
}

// Everything that's genuinely generic across both kinds (time editing,
// period editing, the read/save-to-device flow itself) operates on
// this union rather than PumpScheduleEntry specifically -- see
// isPumpEntry() below for the one place that needs to tell them apart.
type ScheduleEntry = PumpScheduleEntry | LightScheduleEntry;

function isPumpEntry(entry: ScheduleEntry): entry is PumpScheduleEntry {
  return "mode" in entry;
}

// Bit values confirmed against python-mobius's own documentation
// (06-light-schedule.md's own flags table -- shared by light and pump
// points, same wire framing for both). ACTIVE(1) is not a period on
// its own; every real point has it set, so it's excluded here.
// SUNRISE/SUNSET are combination flags that also include the NIGHT
// bit, so they're checked first -- checking NIGHT alone first would
// misclassify every sunrise/sunset point as plain Night.
type Period = "day" | "night" | "sunrise" | "sunset";

function periodForFlags(flags: number): Period {
  if ((flags & 6) === 6) return "sunrise";
  if ((flags & 10) === 10) return "sunset";
  if (flags & 2) return "night";
  return "day";
}

function periodLabel(period: Period): string {
  switch (period) {
    case "sunrise":
      return localize("schedule_card.period_sunrise");
    case "sunset":
      return localize("schedule_card.period_sunset");
    case "night":
      return localize("schedule_card.period_night");
    default:
      return localize("schedule_card.period_day");
  }
}

// h:mm, 24h wall-clock -- time_minutes is minutes since midnight
// (0-1439), not a real Date, so this doesn't need locale-aware
// formatTime() the way the chart's own hour labels do (there's no
// timezone or date involved, just a wall-clock offset).
function formatMinutes(minutes: number): string {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return `${h}:${String(m).padStart(2, "0")}`;
}

// A fixed, universal enum (python-mobius's own RampType) -- unlike
// PumpMode/channels, its own valid values don't vary by pump model,
// so unlike those this doesn't need to come from the backend at all.
const RAMP_TYPES = ["Sinusoidal", "Logarithmic", "Linear"];

// The reverse of periodForFlags() -- ACTIVE(1) is always set on a
// real point (a point without it is a padding entry, never something
// a person edits), combined with the bits for the chosen period.
const PERIOD_FLAGS: Record<Period, number> = { day: 1, night: 3, sunrise: 7, sunset: 11 };

// The app's own UI never shows a raw PhaseShift value or separate
// "Sync"/"EcoSmartBack" entries to choose between -- confirmed from
// the decompiled app's own PumpMode.text(): Sync's own display name
// is picked from PhaseShift alone (180 -> "Anti-Sync", anything else
// -> "Sync"), and EcoSmartBack is always just "EcoSmart Back" with no
// phase-based split of its own at all. So exactly three choices ever
// reach a person, never four and never a raw phase number:
// Sync, Anti-Sync, EcoSmart Back. These helpers reproduce that
// collapsing here, rather than exposing PhaseShift as a plain numeric
// field the way every other param is shown.
type DisplayMode = "Sync" | "AntiSync" | string; // string covers EcoSmartBack and every non-child mode as-is

function displayModeFor(mode: string, phaseShift: unknown): DisplayMode {
  if (mode !== "Sync") return mode;
  return phaseShift === 180 ? "AntiSync" : "Sync";
}

function displayModeLabel(displayMode: DisplayMode): string {
  if (displayMode === "AntiSync") return localize("schedule_card.anti_sync");
  if (displayMode === "EcoSmartBack") return localize("schedule_card.ecosmart_back");
  return displayMode;
}

// The dropdown's own option list -- Sync expands to two entries
// (Sync/Anti-Sync), everything else (including EcoSmartBack) passes
// through as a single entry, matching the real modes this pump
// actually supports.
function displayModeOptions(modes: string[]): DisplayMode[] {
  return modes.flatMap((m) => (m === "Sync" ? (["Sync", "AntiSync"] as DisplayMode[]) : [m]));
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

  @state() private _view: "glance" | "edit" = "glance";
  @state() private _scheduleLoading = false;
  @state() private _scheduleError?: string;
  @state() private _schedulePoints: ScheduleEntry[] = [];

  // The actual device write -- everything up to this point (per-point
  // Save) only ever touches _schedulePoints in memory. A brief
  // success message auto-clears itself; an error persists until the
  // next save attempt, so it doesn't disappear before someone's had a
  // chance to read it.
  @state() private _savingSchedule = false;
  @state() private _saveScheduleError?: string;
  @state() private _saveScheduleSucceeded = false;

  // .mob export/import -- both local browser file operations, never
  // touching the device directly. Import replaces _schedulePoints
  // wholesale (a .mob file represents a whole schedule, not a single
  // point) but doesn't write anything on its own -- the person still
  // reviews and presses Save schedule to device afterward, same as
  // any other local edit.
  @state() private _exportingMob = false;
  @state() private _importingMob = false;
  @state() private _mobError?: string;

  // Which point (by index into _schedulePoints) is currently expanded
  // for editing -- null when none is. _workingPoint is a separate,
  // mutable copy of that point's own data, so edits in progress don't
  // touch _schedulePoints (and thus don't affect the read-only list's
  // own rendering) until Save is actually pressed.
  @state() private _editingIndex: number | null = null;
  @state() private _workingPoint?: ScheduleEntry;

  // True only when the point currently open for editing was just
  // created by Add point, not an existing one someone opened to
  // modify. Cancel needs to tell these apart: discarding an edit to
  // an existing point means leaving it as it was, but discarding a
  // brand new point means removing it -- Add already appended a real
  // (if default-valued) entry to _schedulePoints before editing even
  // starts, so without this a cancelled Add would silently leave that
  // half-configured point sitting in the list.
  @state() private _isNewPoint = false;

  // For the parent-pump picker -- every pump member on the same tank
  // other than this device itself. Populated in _resolveGroup(); see
  // its own comment there for why no separate fetch is needed.
  @state() private _otherPumps: ScheduleGroupMember[] = [];

  // Not @state -- this is a plain timer handle, not something that
  // should itself trigger a re-render when it changes.
  private _intensityDebounceHandle?: ReturnType<typeof setTimeout>;

  // The device_id resolution was last run for -- avoids re-resolving
  // on every single hass update (which happens on every poll cycle
  // for ANY entity in the whole system, not just this card's own
  // ones); only actually re-runs when the configured device_id itself
  // changes, or after a failed attempt where hass wasn't ready yet.
  private _resolvedFor?: string;

  // Re-fetches channel history on a timer -- a single fetch at
  // resolution time would otherwise leave the chart frozen at
  // whatever moment the card first loaded: new data points never
  // arrive on their own the way live entity state does, since history
  // is a point-in-time query, not something hass itself pushes
  // updates for. Cleared on disconnect so it doesn't keep firing (and
  // holding a reference to this card) after the card leaves the DOM.
  private _historyRefreshHandle?: ReturnType<typeof setInterval>;
  private static readonly HISTORY_REFRESH_INTERVAL_MS = 5 * 60 * 1000;

  public disconnectedCallback(): void {
    super.disconnectedCallback();
    clearInterval(this._historyRefreshHandle);
  }

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

      // For the parent-pump picker (Sync/EcoSmartBack's own
      // ParentSerial field) -- every pump member across every OTHER
      // pump group on this same tank, excluding this device itself.
      // Comes from this same response (every group on the tank, not
      // just the matched one), so no separate round-trip is needed.
      this._otherPumps = response.groups
        .filter((g) => g.kind === "pump")
        .flatMap((g) => g.members)
        .filter((m) => m.device_id !== deviceId);

      clearInterval(this._historyRefreshHandle);
      if (group.kind === "light") {
        // Deliberately not awaited -- the glance view itself doesn't
        // depend on history to render (readings/slider/scene banner
        // all come from live state), so a slow history query
        // shouldn't hold up everything else. The chart area just
        // shows its own loading state until this resolves.
        this._fetchChannelHistory(group);
        this._historyRefreshHandle = setInterval(
          () => this._fetchChannelHistory(group),
          MobiusScheduleCard.HISTORY_REFRESH_INTERVAL_MS,
        );
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

  private _closeEdit(): void {
    // Same reasoning as _cancelEditingPoint() -- leaving edit mode
    // entirely while a freshly-added point is still mid-edit must
    // remove that point, not leave a half-configured default sitting
    // in the list the person never actually confirmed.
    if (this._isNewPoint && this._editingIndex != null) {
      this._schedulePoints = this._schedulePoints.filter((_, i) => i !== this._editingIndex);
    }
    this._view = "glance";
    this._editingIndex = null;
    this._workingPoint = undefined;
    this._isNewPoint = false;
    this._saveScheduleError = undefined;
    this._saveScheduleSucceeded = false;
  }

  private _startEditingPoint(index: number): void {
    this._editingIndex = index;
    this._isNewPoint = false;
    // A real clone, not a reference -- params/channels is itself an
    // object, and mutating it in place would leak edits-in-progress
    // into _schedulePoints (and thus the read-only list) before Save.
    const point = this._schedulePoints[index];
    this._workingPoint = isPumpEntry(point)
      ? { ...point, params: { ...point.params } }
      : { ...point, channels: { ...point.channels } };
  }

  private _cancelEditingPoint(): void {
    // A fresh Add already appended a real (default-valued) entry to
    // _schedulePoints before editing started -- cancelling it needs
    // to remove that entry entirely, not just close the form on top
    // of it, or a half-configured point would silently stay in the
    // list. Cancelling an edit to an EXISTING point, by contrast,
    // should leave that point exactly as it was.
    if (this._isNewPoint && this._editingIndex != null) {
      this._schedulePoints = this._schedulePoints.filter((_, i) => i !== this._editingIndex);
    }
    this._editingIndex = null;
    this._workingPoint = undefined;
    this._isNewPoint = false;
  }

  private _saveEditingPoint(): void {
    if (this._editingIndex == null || !this._workingPoint) return;
    const points = [...this._schedulePoints];
    points[this._editingIndex] = this._workingPoint;
    this._schedulePoints = points;
    this._editingIndex = null;
    this._workingPoint = undefined;
    this._isNewPoint = false;
  }

  // Generic across both kinds -- deletes whichever point is currently
  // open for editing. Local-only, same as every other point edit:
  // nothing reaches the device until Save schedule to device is
  // pressed, so there's no separate confirmation step here -- the
  // save action itself is the actual point of no return.
  private _deleteEditingPoint(): void {
    if (this._editingIndex == null) return;
    this._schedulePoints = this._schedulePoints.filter((_, i) => i !== this._editingIndex);
    this._editingIndex = null;
    this._workingPoint = undefined;
    this._isNewPoint = false;
  }

  private _updateWorkingPointTime(minutes: number): void {
    if (!this._workingPoint) return;
    this._workingPoint = { ...this._workingPoint, time_minutes: minutes };
  }

  private _updateWorkingPointPeriod(period: Period): void {
    if (!this._workingPoint) return;
    this._workingPoint = { ...this._workingPoint, flags: PERIOD_FLAGS[period] };
  }

  // Pump-specific (mode/params only exist on PumpScheduleEntry) --
  // only ever called from the pump edit form, itself only reachable
  // when _group.kind === "pump", so the cast is safe by construction
  // rather than needing a runtime check on every call.
  private _updateWorkingPumpMode(displayMode: DisplayMode): void {
    const working = this._workingPoint as PumpScheduleEntry | undefined;
    if (!working || !this._group) return;

    // Anti-Sync isn't a real mode at all -- it's Sync with
    // PhaseShift=180 (see the DisplayMode helpers above for the full
    // reasoning). EcoSmartBack's own PhaseShift is never shown to a
    // person either, so it gets the same "not 180" default Sync
    // itself uses absent a choice -- the app's own UI never exposes a
    // way to set it any other way, so this is the only value there's
    // ever a reason to send.
    const realMode = displayMode === "AntiSync" ? "Sync" : displayMode;
    const presetPhaseShift =
      displayMode === "AntiSync" ? 180 : realMode === "Sync" || realMode === "EcoSmartBack" ? 0 : undefined;

    const paramNames = this._group.mode_params?.[realMode] ?? [];
    // Values for params the new mode shares with the old one carry
    // over (e.g. switching Lagoon -> ReefCrest keeps MaxSpeed); a
    // param the new mode needs that the old one didn't have gets a
    // sensible zero-ish default rather than being left undefined,
    // since encode() (python-mobius) raises if a required param is
    // ever missing at save time.
    const params: Record<string, unknown> = {};
    for (const name of paramNames) {
      if (name === "PhaseShift" && presetPhaseShift !== undefined) {
        params[name] = presetPhaseShift;
      } else if (name in working.params) {
        params[name] = working.params[name];
      } else if (name === "RampType") {
        params[name] = RAMP_TYPES[0];
      } else if (name === "ParentSerial") {
        params[name] = this._otherPumps[0]?.serial ?? "";
      } else {
        params[name] = 0;
      }
    }
    this._workingPoint = { ...working, mode: realMode, params };
  }

  private _updateWorkingPumpParam(name: string, value: string | number): void {
    const working = this._workingPoint as PumpScheduleEntry | undefined;
    if (!working) return;
    this._workingPoint = { ...working, params: { ...working.params, [name]: value } };
  }

  private async _openEdit(): Promise<void> {
    this._view = "edit";
    this._scheduleLoading = true;
    this._scheduleError = undefined;
    try {
      const response = await this.hass.callWS<{ points: ScheduleEntry[] }>({
        type: "mobius/read_schedule_group",
        device_id: this._config!.device_id,
      });
      this._schedulePoints = response.points;
    } catch (err) {
      this._scheduleError = err instanceof Error ? err.message : String(err);
      this._schedulePoints = [];
    } finally {
      this._scheduleLoading = false;
    }
  }

  private async _saveScheduleToDevice(): Promise<void> {
    this._savingSchedule = true;
    this._saveScheduleError = undefined;
    this._saveScheduleSucceeded = false;
    try {
      // Sent exactly as-is -- read_schedule_group already handed the
      // card this same shape (ParentSerial, not Master; already
      // decoded params), and the write service expects that same
      // shape back, translating ParentSerial to Master internally
      // itself. No transformation needed on this end at all.
      await this.hass.callService("mobius", "write_schedule_group", {
        device_id: this._config!.device_id,
        points: this._schedulePoints,
      });
      this._saveScheduleSucceeded = true;
      setTimeout(() => {
        this._saveScheduleSucceeded = false;
      }, 4000);
    } catch (err) {
      this._saveScheduleError = err instanceof Error ? err.message : String(err);
    } finally {
      this._savingSchedule = false;
    }
  }

  private async _exportMob(): Promise<void> {
    this._exportingMob = true;
    this._mobError = undefined;
    try {
      // The card never encodes/decodes primitiveData itself -- the
      // response is already the finished .mob file's own JSON
      // content, built server-side by python-mobius.
      const response = await this.hass.callWS<{ mob: unknown }>({
        type: "mobius/export_schedule_group_mob",
        device_id: this._config!.device_id,
      });
      const blob = new Blob([JSON.stringify(response.mob, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${this._config!.device_id}.mob`;
      link.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      this._mobError = err instanceof Error ? err.message : String(err);
    } finally {
      this._exportingMob = false;
    }
  }

  private _triggerMobFilePicker(): void {
    this._mobError = undefined;
    this.shadowRoot?.querySelector<HTMLInputElement>(".mob-file-input")?.click();
  }

  private async _handleMobFileSelected(e: Event): Promise<void> {
    const input = e.target as HTMLInputElement;
    const file = input.files?.[0];
    input.value = ""; // allows re-selecting the same file name later
    if (!file) return;

    // An explicit extension check, not just the file input's own
    // accept=".mob" attribute -- that's only a picker hint and is
    // trivial for a person to bypass (drag-and-drop, "all files").
    if (!file.name.toLowerCase().endsWith(".mob")) {
      this._mobError = localize("schedule_card.mob_wrong_extension");
      return;
    }

    this._importingMob = true;
    this._mobError = undefined;
    try {
      const text = await file.text();
      let mob: unknown;
      try {
        mob = JSON.parse(text);
      } catch {
        throw new Error(localize("schedule_card.mob_invalid_json"));
      }
      const response = await this.hass.callWS<{ points: ScheduleEntry[] }>({
        type: "mobius/parse_schedule_mob",
        device_id: this._config!.device_id,
        mob,
      });
      // Replaces the whole schedule -- a .mob file represents an
      // entire schedule, not a single point. Nothing is written to
      // the device yet; the person still reviews this and presses
      // Save schedule to device themselves, same as any local edit.
      this._schedulePoints = response.points;
      this._editingIndex = null;
      this._workingPoint = undefined;
    } catch (err) {
      this._mobError = err instanceof Error ? err.message : String(err);
    } finally {
      this._importingMob = false;
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
        <button class="edit-button" @click=${() => this._openEdit()}>${localize("schedule_card.edit_schedule")}</button>
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

  // Recomputing this is real work (every point in every channel's own
  // history gets transformed to SVG coordinates and re-stringified) --
  // and render() itself runs far more often than that data actually
  // changes: hass is reassigned as a brand new object on every state
  // change anywhere in the whole system (not just this card's own
  // entities), and Lit's default change detection is reference-based,
  // so this card re-renders on nearly every Home Assistant event.
  // Skipping recomputation whenever the three things that actually
  // affect the chart's own output (which member's data, the history
  // itself, and the locale used for hour labels) haven't changed since
  // the last render avoids doing that work dozens of times a minute
  // for a result that would come out identical every time anyway.
  private _lastChartKey?: string;
  private _lastChartResult?: TemplateResult;

  private _renderChannelChart(sourceMember: ScheduleGroupMember) {
    if (this._historyLoading) {
      return html`<div class="chart-status">${localize("schedule_card.loading_history")}</div>`;
    }

    const lang = this.hass?.locale?.language;
    const key = `${sourceMember.device_id}|${lang}`;
    if (key === this._lastChartKey && this._channelHistoryByEntity === this._lastChartHistoryRef) {
      return this._lastChartResult;
    }

    const result = this._computeChannelChart(sourceMember);
    this._lastChartKey = key;
    this._lastChartHistoryRef = this._channelHistoryByEntity;
    this._lastChartResult = result;
    return result;
  }

  private _lastChartHistoryRef?: Record<string, HistoryPoint[]>;

  private _computeChannelChart(sourceMember: ScheduleGroupMember) {
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
        <button class="edit-button" @click=${() => this._openEdit()}>${localize("schedule_card.edit_schedule")}</button>
      </ha-card>
    `;
  }

  // The "ha-card + back button (+ title)" wrapper shared by every
  // state of the edit view (error, loading, and the real content) --
  // reused as-is by light's own edit view once it exists, since none
  // of this cares what kind of group is being edited.
  private _renderEditShell(content: unknown) {
    return html`
      <ha-card>
        <div class="edit-header">
          <button class="back-button" @click=${() => this._closeEdit()}>${localize("schedule_card.back")}</button>
          <div class="title">${localize("schedule_card.edit_schedule")}</div>
        </div>
        ${content}
      </ha-card>
    `;
  }

  // The save-to-device button plus its own status messages -- entirely
  // generic (write_schedule_group takes whatever's in _schedulePoints
  // as-is, regardless of kind), reused unchanged by light's own edit
  // view.
  private _renderSaveScheduleControls() {
    return html`
      ${this._saveScheduleError ? html`<div class="save-schedule-error">${this._saveScheduleError}</div>` : nothing}
      ${
        this._saveScheduleSucceeded
          ? html`<div class="save-schedule-success">${localize("schedule_card.schedule_saved")}</div>`
          : nothing
      }
      <button
        class="save-schedule-button"
        ?disabled=${this._savingSchedule || this._editingIndex != null}
        @click=${() => this._saveScheduleToDevice()}
      >
        ${this._savingSchedule ? localize("schedule_card.saving_schedule") : localize("schedule_card.save_schedule")}
      </button>
    `;
  }

  // .mob download/load -- fully generic (export/parse_schedule_mob
  // both just take/return whatever ScheduleEntry data the card
  // already works with either way), reused unchanged by both kinds.
  private _renderMobControls() {
    return html`
      ${this._mobError ? html`<div class="save-schedule-error">${this._mobError}</div>` : nothing}
      <div class="mob-controls">
        <button
          class="mob-button"
          ?disabled=${this._exportingMob || this._editingIndex != null}
          @click=${() => this._exportMob()}
        >
          ${this._exportingMob ? localize("schedule_card.exporting_mob") : localize("schedule_card.download_mob")}
        </button>
        <button
          class="mob-button"
          ?disabled=${this._importingMob || this._editingIndex != null}
          @click=${() => this._triggerMobFilePicker()}
        >
          ${this._importingMob ? localize("schedule_card.importing_mob") : localize("schedule_card.load_mob")}
        </button>
        <input
          type="file"
          class="mob-file-input"
          accept=".mob"
          style="display: none"
          @change=${(e: Event) => this._handleMobFileSelected(e)}
        />
      </div>
    `;
  }

  // Fully generic across both kinds -- error/loading states, the
  // point list, and the save controls are identical either way; the
  // only thing that differs is what a single point's own edit form
  // looks like, passed in rather than hardcoded here.
  private _renderScheduleEditView(renderPointEditForm: () => unknown, onAddPoint: () => void) {
    if (this._scheduleError) {
      return this._renderEditShell(html`<div class="warning">${this._scheduleError}</div>`);
    }
    if (this._scheduleLoading) {
      return this._renderEditShell(html`<div class="loading">${localize("schedule_card.loading")}</div>`);
    }

    return this._renderEditShell(html`
      <div class="point-list">
        ${this._schedulePoints.map((point, index) =>
          this._editingIndex === index ? renderPointEditForm() : this._renderPointRow(point, index),
        )}
      </div>
      <button class="add-point-button" ?disabled=${this._editingIndex != null} @click=${onAddPoint}>
        ${localize("schedule_card.add_point")}
      </button>
      ${this._renderMobControls()} ${this._renderSaveScheduleControls()}
    `);
  }

  private _renderPumpEditView() {
    return this._renderScheduleEditView(
      () => this._renderPumpPointEditForm(),
      () => this._addPumpPoint(),
    );
  }

  // Generic across both kinds -- time, period, and a one-line summary
  // (the mode name for a pump point; light's own summary, once it
  // exists, would describe its channels instead). isPumpEntry() is
  // the only place this needs to tell the two apart at all.
  private _renderPointRow(point: ScheduleEntry, index: number) {
    const period = periodForFlags(point.flags);
    const summary = isPumpEntry(point) ? point.mode : Object.keys(point.channels).join(", ");
    return html`
      <button class="point-row" @click=${() => this._startEditingPoint(index)}>
        <span class="point-time">${formatMinutes(point.time_minutes)}</span>
        <span class="point-period period-${period}">${periodLabel(period)}</span>
        <span class="point-mode">${summary}</span>
      </button>
    `;
  }

  // The time + period inputs -- fully generic (both operate on
  // time_minutes/flags, present on every ScheduleEntry regardless of
  // kind), reused as-is inside light's own point edit form.
  private _renderTimeAndPeriodFields(point: ScheduleEntry) {
    const period = periodForFlags(point.flags);
    const [hours, minutes] = [Math.floor(point.time_minutes / 60), point.time_minutes % 60];
    return html`
      <div class="edit-row">
        <label>
          ${localize("schedule_card.time")}
          <input
            type="time"
            .value=${`${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}`}
            @change=${(e: Event) => {
              const [h, m] = (e.target as HTMLInputElement).value.split(":").map(Number);
              this._updateWorkingPointTime(h * 60 + m);
            }}
          />
        </label>
        <label>
          ${localize("schedule_card.period")}
          <select
            .value=${period}
            @change=${(e: Event) => this._updateWorkingPointPeriod((e.target as HTMLSelectElement).value as Period)}
          >
            ${(["day", "night", "sunrise", "sunset"] as Period[]).map(
              (p) => html`<option value=${p} ?selected=${p === period}>${periodLabel(p)}</option>`,
            )}
          </select>
        </label>
      </div>
    `;
  }

  // Cancel/Delete/Save -- fully generic (delete/save/cancel all
  // operate on _editingIndex/_workingPoint directly, neither cares
  // what kind of point they're holding), reused by both edit forms.
  private _renderEditActions() {
    return html`
      <div class="edit-actions">
        <button class="cancel-button" @click=${() => this._cancelEditingPoint()}>
          ${localize("schedule_card.cancel")}
        </button>
        <button class="delete-point-button" @click=${() => this._deleteEditingPoint()}>
          ${localize("schedule_card.delete")}
        </button>
        <button class="save-point-button" @click=${() => this._saveEditingPoint()}>
          ${localize("schedule_card.save")}
        </button>
      </div>
    `;
  }

  // Pump-specific from here down -- mode dropdown and its own dynamic
  // param fields. Light's own point edit form (channel sliders
  // instead) is a sibling of this method, not a variant of it; both
  // share _renderTimeAndPeriodFields/_renderEditShell/
  // _renderSaveScheduleControls/_renderPointRow above.
  private _addPumpPoint(): void {
    const firstMode = this._group?.modes?.[0];
    if (!firstMode) return;
    const newPoint: PumpScheduleEntry = { time_minutes: 0, flags: 1, mode: firstMode, params: {} };
    this._schedulePoints = [...this._schedulePoints, newPoint];
    this._startEditingPoint(this._schedulePoints.length - 1);
    this._isNewPoint = true;
    // Reuses the exact same "populate this mode's own real params"
    // logic a person switching mode on an existing point already
    // gets -- a brand new point is no different from any other point
    // whose mode just changed from nothing.
    this._updateWorkingPumpMode(displayModeFor(firstMode, undefined));
  }

  private _renderPumpPointEditForm() {
    const point = this._workingPoint as PumpScheduleEntry;
    const modes = this._group?.modes ?? [];
    const displayModes = displayModeOptions(modes);
    const currentDisplayMode = displayModeFor(point.mode, point.params.PhaseShift);
    // PhaseShift is never shown as its own field -- it's fully implied
    // by which of Sync/Anti-Sync/EcoSmart Back was chosen above (see
    // the DisplayMode helpers' own reasoning).
    const paramNames = (this._group?.mode_params?.[point.mode] ?? []).filter((name) => name !== "PhaseShift");

    return html`
      <div class="point-edit-form">
        ${this._renderTimeAndPeriodFields(point)}
        <label class="mode-label">
          ${localize("schedule_card.mode")}
          <select
            .value=${currentDisplayMode}
            @change=${(e: Event) => this._updateWorkingPumpMode((e.target as HTMLSelectElement).value)}
          >
            ${displayModes.map(
              (dm) => html`<option value=${dm} ?selected=${dm === currentDisplayMode}>${displayModeLabel(dm)}</option>`,
            )}
          </select>
        </label>
        ${paramNames.map((name) => this._renderPumpParamInput(name, point.params[name]))} ${this._renderEditActions()}
      </div>
    `;
  }

  private _renderPumpParamInput(name: string, value: unknown) {
    if (name === "RampType") {
      return html`
        <label class="param-label">
          ${name}
          <select
            .value=${String(value)}
            @change=${(e: Event) => this._updateWorkingPumpParam(name, (e.target as HTMLSelectElement).value)}
          >
            ${RAMP_TYPES.map((rt) => html`<option value=${rt} ?selected=${rt === value}>${rt}</option>`)}
          </select>
        </label>
      `;
    }
    if (name === "ParentSerial") {
      return html`
        <label class="param-label">
          ${localize("schedule_card.parent_pump")}
          ${
            this._otherPumps.length === 0
              ? html`<div class="no-other-pumps">${localize("schedule_card.no_other_pumps")}</div>`
              : html`
                  <select
                    .value=${String(value ?? "")}
                    @change=${(e: Event) => this._updateWorkingPumpParam(name, (e.target as HTMLSelectElement).value)}
                  >
                    ${this._otherPumps.map(
                      (pump) =>
                        html`<option value=${pump.serial} ?selected=${pump.serial === value}>${pump.name}</option>`,
                    )}
                  </select>
                `
          }
        </label>
      `;
    }
    // Confirmed in the app's own PumpPrimitive.getReverse(): a
    // negative MaxSpeed/MinSpeed reverses rotation direction, on
    // pumps whose model actually supports it (sliderSettings'
    // supportsReverse). Which models do isn't exposed by this
    // integration's own backend, so the hint is deliberately phrased
    // as "on supported pumps" rather than claiming this always does
    // something -- the value itself already round-trips correctly
    // either way (python-mobius passes it through as-is), this is
    // purely about not leaving a negative-number field unexplained.
    const isSpeedParam = name === "MaxSpeed" || name === "MinSpeed";
    return html`
      <label class="param-label">
        ${name}
        <input
          type="number"
          .value=${String(value ?? 0)}
          title=${isSpeedParam ? localize("schedule_card.reverse_hint") : nothing}
          @change=${(e: Event) => this._updateWorkingPumpParam(name, Number((e.target as HTMLInputElement).value))}
        />
        ${isSpeedParam ? html`<span class="field-hint">${localize("schedule_card.reverse_hint")}</span>` : nothing}
      </label>
    `;
  }

  private _renderLightEditView() {
    return this._renderScheduleEditView(
      () => this._renderLightPointEditForm(),
      () => this._addLightPoint(),
    );
  }

  // Light-specific from here down -- one intensity slider per real
  // channel this group actually has, instead of a mode dropdown.
  // Shares exactly the same _renderTimeAndPeriodFields/_renderEditShell/
  // _renderSaveScheduleControls/_renderPointRow/_renderScheduleEditView
  // the pump-specific section above does -- everything above this
  // point in the file is what made that possible.
  private _addLightPoint(): void {
    const channels = this._group?.channels ?? [];
    if (channels.length === 0) return;
    const newPoint: LightScheduleEntry = {
      time_minutes: 0,
      flags: 1,
      channels: Object.fromEntries(channels.map((c) => [c, 0])),
    };
    this._schedulePoints = [...this._schedulePoints, newPoint];
    this._startEditingPoint(this._schedulePoints.length - 1);
    this._isNewPoint = true;
  }

  private _updateWorkingLightChannel(channel: string, percent: number): void {
    const working = this._workingPoint as LightScheduleEntry | undefined;
    if (!working) return;
    this._workingPoint = { ...working, channels: { ...working.channels, [channel]: percent } };
  }

  private _renderLightPointEditForm() {
    const point = this._workingPoint as LightScheduleEntry;
    const channels = this._group?.channels ?? [];

    return html`
      <div class="point-edit-form">
        ${this._renderTimeAndPeriodFields(point)}
        ${channels.map((channel) => {
          const value = point.channels[channel] ?? 0;
          return html`
            <label class="channel-slider-label">
              <span class="channel-slider-name" style="color: ${channelColor(channel)}">${channel}</span>
              <span class="channel-slider-value">${value}%</span>
              <input
                type="range"
                min="0"
                max="100"
                .value=${String(value)}
                @input=${(e: Event) =>
                  this._updateWorkingLightChannel(channel, Number((e.target as HTMLInputElement).value))}
              />
            </label>
          `;
        })}
        ${this._renderEditActions()}
      </div>
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

    if (this._view === "edit") {
      return this._group.kind === "pump" ? this._renderPumpEditView() : this._renderLightEditView();
    }

    if (this._group.kind === "pump") {
      return this._renderPumpGlance();
    }

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
    .edit-header {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 14px;
    }
    .back-button {
      background: none;
      border: none;
      color: var(--primary-color);
      font-family: inherit;
      font-size: 0.9em;
      font-weight: 500;
      cursor: pointer;
      padding: 4px 0;
    }
    .point-list {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .point-row {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 8px 10px;
      border-radius: 8px;
      background: var(--secondary-background-color, rgba(0, 0, 0, 0.03));
      font-size: 0.9em;
      width: 100%;
      border: none;
      color: inherit;
      font-family: inherit;
      cursor: pointer;
      text-align: left;
    }
    .point-row:hover {
      background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.08);
    }
    .point-edit-form {
      display: flex;
      flex-direction: column;
      gap: 10px;
      padding: 12px;
      border-radius: 8px;
      border: 1px solid var(--primary-color);
      background: var(--card-background-color);
    }
    .edit-row {
      display: flex;
      gap: 10px;
    }
    .edit-row label,
    .mode-label,
    .param-label {
      display: flex;
      flex-direction: column;
      gap: 4px;
      font-size: 0.8em;
      color: var(--secondary-text-color);
      flex: 1;
    }
    .field-hint {
      font-size: 0.85em;
      font-style: italic;
      color: var(--secondary-text-color);
    }
    .no-other-pumps {
      font-size: 0.95em;
      color: var(--error-color, #db4437);
      padding: 4px 0;
    }
    .edit-row input,
    .edit-row select,
    .mode-label select,
    .param-label input,
    .param-label select {
      padding: 6px 8px;
      border-radius: 6px;
      border: 1px solid var(--divider-color);
      background: var(--card-background-color);
      color: var(--primary-text-color);
      font-family: inherit;
      font-size: 1em;
    }
    .edit-actions {
      display: flex;
      gap: 8px;
      margin-top: 4px;
    }
    .cancel-button,
    .delete-point-button,
    .save-point-button {
      flex: 1;
      padding: 8px 0;
      border-radius: 8px;
      font-family: inherit;
      font-size: 0.9em;
      font-weight: 500;
      cursor: pointer;
    }
    .cancel-button {
      background: none;
      border: 1px solid var(--divider-color);
      color: var(--primary-text-color);
    }
    .delete-point-button {
      background: none;
      border: 1px solid var(--error-color, #db4437);
      color: var(--error-color, #db4437);
    }
    .save-point-button {
      background: var(--primary-color);
      border: none;
      color: var(--text-primary-color, #fff);
    }
    .add-point-button {
      width: 100%;
      margin-top: 10px;
      padding: 10px 0;
      border-radius: 10px;
      border: 1px dashed var(--divider-color);
      background: none;
      color: var(--primary-color);
      font-family: inherit;
      font-size: 0.9em;
      font-weight: 500;
      cursor: pointer;
    }
    .add-point-button:disabled {
      opacity: 0.5;
      cursor: default;
    }
    .mob-controls {
      display: flex;
      gap: 8px;
      margin-top: 10px;
    }
    .mob-button {
      flex: 1;
      padding: 8px 0;
      border-radius: 10px;
      border: 1px solid var(--divider-color);
      background: none;
      color: var(--primary-text-color);
      font-family: inherit;
      font-size: 0.85em;
      font-weight: 500;
      cursor: pointer;
    }
    .mob-button:disabled {
      opacity: 0.5;
      cursor: default;
    }
    .save-schedule-button {
      width: 100%;
      margin-top: 14px;
      padding: 12px 0;
      border-radius: 10px;
      border: none;
      background: var(--primary-color);
      color: var(--text-primary-color, #fff);
      font-family: inherit;
      font-size: 0.95em;
      font-weight: 500;
      cursor: pointer;
    }
    .save-schedule-button:disabled {
      opacity: 0.5;
      cursor: default;
    }
    .save-schedule-error {
      margin-top: 12px;
      padding: 10px;
      border-radius: 8px;
      background: rgba(219, 68, 55, 0.1);
      color: var(--error-color, #db4437);
      font-size: 0.9em;
    }
    .save-schedule-success {
      margin-top: 12px;
      padding: 10px;
      border-radius: 8px;
      background: rgba(67, 160, 71, 0.1);
      color: #43a047;
      font-size: 0.9em;
    }
    .point-time {
      font-variant-numeric: tabular-nums;
      color: var(--primary-text-color);
      font-weight: 500;
      min-width: 44px;
    }
    .channel-slider-label {
      display: grid;
      grid-template-columns: 1fr auto;
      align-items: center;
      gap: 4px 8px;
      font-size: 0.85em;
    }
    .channel-slider-name {
      font-weight: 500;
    }
    .channel-slider-value {
      color: var(--secondary-text-color);
      font-variant-numeric: tabular-nums;
      text-align: right;
    }
    .channel-slider-label input[type="range"] {
      grid-column: 1 / -1;
      width: 100%;
      accent-color: var(--primary-color);
    }
    .point-period {
      font-size: 0.8em;
      padding: 2px 8px;
      border-radius: 10px;
      background: var(--divider-color);
      color: var(--secondary-text-color);
    }
    .point-period.period-night {
      background: #37474f;
      color: #e0e0e0;
    }
    .point-period.period-sunrise {
      background: #ffcc80;
      color: #5d4037;
    }
    .point-period.period-sunset {
      background: #ff8a65;
      color: #4e342e;
    }
    .point-mode {
      color: var(--secondary-text-color);
      margin-left: auto;
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
