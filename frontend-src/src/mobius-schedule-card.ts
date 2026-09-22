import { LitElement, html, css, svg, nothing, type TemplateResult } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import type { HomeAssistant, LovelaceCardConfig } from "custom-card-helpers";
import { formatTime } from "custom-card-helpers";
import { localize } from "./localize/localize";
import { CountdownTicker, formatDuration, sceneRemainingSeconds } from "./format";

/**
 * mobius-schedule-card
 *
 * Shows and edits the schedule of one schedule group. Configured with the
 * device_id of a light or pump (not the tank): the card finds the device's
 * tank through via_device_id, calls mobius/resolve_schedule_groups for it and
 * shows the group containing the device. Independent light groups on one
 * tank need one card each.
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
const CHART_HEIGHT = 200;

// Display color per channel, keyed by the lowercased VisualID name (e.g.
// "deepred"). Only cosmetic. Covers every lighting channel VisualID defines
// (not Brightness or the Status*/probability entries); other names get a
// color from FALLBACK_PALETTE by hash.
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

// custom-card-helpers' HomeAssistant type lacks the device registry that the
// real hass object has.
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
  lunar_switch_entity_id?: string | null;
  speed_entity_id?: string | null;
  flow_entity_id?: string | null;
  mode_entity_id?: string | null;
  supports_reverse?: boolean | null;
}

interface ScheduleGroup {
  kind: "light" | "pump";
  group_mask: number | null;
  members: ScheduleGroupMember[];
  channels?: string[];
  modes?: string[];
  mode_params?: Record<string, string[]>;
  active_scene: { name: string; duration_seconds: number; ends_at?: string | null } | null;
  scene_entity_id: string | null;
  schedule_intensity?: number | null;
}

// Compressed state format of history/history_during_period: s = state,
// lc = last changed, lu = last updated (omitted when equal to lc).
interface CompressedStateEntry {
  s: string;
  lu?: number;
  lc?: number;
}

interface HistoryPoint {
  t: number; // ms since epoch
  v: number;
}

// pump_schedule_to_dict() entry (python-mobius).
interface PumpScheduleEntry {
  time_minutes: number;
  flags: number;
  mode: string;
  params: Record<string, unknown>;
}

// light_schedule_to_dict() entry (python-mobius).
interface LightScheduleEntry {
  time_minutes: number;
  flags: number;
  channels: Record<string, number>;
}

// Time/period editing and reading/saving work on either kind; isPumpEntry()
// tells them apart.
type ScheduleEntry = PumpScheduleEntry | LightScheduleEntry;

function isPumpEntry(entry: ScheduleEntry): entry is PumpScheduleEntry {
  return "mode" in entry;
}

// Point flags (python-mobius 06-light-schedule.md): ACTIVE = 1 on every
// point; SUNRISE (6) and SUNSET (10) include the NIGHT bit (2), so they are
// checked first.
type Period = "day" | "night" | "sunrise" | "sunset";

function periodForFlags(flags: number): Period {
  if ((flags & 6) === 6) return "sunrise";
  if ((flags & 10) === 10) return "sunset";
  if (flags & 2) return "night";
  return "day";
}

// Localized moon phase name for a moon_phase_icon() value (the lunar
// button's tooltip).
function moonPhaseName(icon: string, hass: HomeAssistant): string {
  const lang = hass?.locale?.language;
  switch (icon) {
    case "mdi:moon-new":
      return localize("schedule_card.moon_phase_new", lang);
    case "mdi:moon-waxing-crescent":
      return localize("schedule_card.moon_phase_waxing_crescent", lang);
    case "mdi:moon-waxing-gibbous":
      return localize("schedule_card.moon_phase_waxing_gibbous", lang);
    case "mdi:moon-full":
      return localize("schedule_card.moon_phase_full", lang);
    case "mdi:moon-waning-gibbous":
      return localize("schedule_card.moon_phase_waning_gibbous", lang);
    case "mdi:moon-waning-crescent":
      return localize("schedule_card.moon_phase_waning_crescent", lang);
    default:
      return "";
  }
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

// h:mm of a time_minutes value (minutes since midnight; no date or time
// zone involved).
function formatMinutes(minutes: number): string {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return `${h}:${String(m).padStart(2, "0")}`;
}

// Whether times are shown with AM/PM, following Home Assistant's Time Format
// setting like its frontend's useAmPm(). The time field uses number inputs
// because a native time input follows the browser locale instead.
function shouldUseAmPm(hass: HomeAssistant): boolean {
  const timeFormat = hass.locale?.time_format as string | undefined;
  if (timeFormat === "12hour" || timeFormat === "am_pm" || timeFormat === "12") return true;
  if (timeFormat === "24hour" || timeFormat === "24") return false;
  // "language" or "system" (or unset) -- follow what the browser's own
  // Intl would use for that locale, same as Home Assistant's own
  // useAmPm() does for these two cases.
  const locale = timeFormat === "language" ? hass.locale?.language : undefined;
  return new Intl.DateTimeFormat(locale, { hour: "numeric" }).resolvedOptions().hour12 ?? false;
}

// A numeric state rounded to an integer (states are unrounded; display
// precision only applies in Home Assistant's own components). Non-numeric
// states are returned as-is.
function roundedState(state: string): string {
  const n = Number(state);
  return Number.isFinite(n) ? String(Math.round(n)) : state;
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function isAvailable(stateObj?: { state: string }): boolean {
  return !!stateObj && stateObj.state !== "unavailable" && stateObj.state !== "unknown";
}

// A clock time in Home Assistant's time format (H:mm without a locale).
function formatClock(date: Date, locale?: HomeAssistant["locale"]): string {
  return locale ? formatTime(date, locale) : `${date.getHours()}:${String(date.getMinutes()).padStart(2, "0")}`;
}

// [channel name, entity_id] of the member's channel sensors that exist.
function channelEntities(member: ScheduleGroupMember): [string, string][] {
  return Object.entries(member.channel_entity_ids ?? {}).filter((entry): entry is [string, string] => !!entry[1]);
}

// Start of the chart window: the last 24 hours up to now, like Home
// Assistant's history charts.
function chartWindowStartMs(): number {
  return Date.now() - 24 * 60 * 60 * 1000;
}

// Timestamps of whole hours divisible by intervalHours within
// [windowStartMs, windowEndMs], for the chart's hour labels.
function niceHourMarks(windowStartMs: number, windowEndMs: number, intervalHours: number): number[] {
  const marks: number[] = [];
  const cursor = new Date(windowStartMs);
  cursor.setMinutes(0, 0, 0);
  while (cursor.getTime() < windowStartMs || cursor.getHours() % intervalHours !== 0) {
    cursor.setHours(cursor.getHours() + 1);
  }
  while (cursor.getTime() <= windowEndMs) {
    marks.push(cursor.getTime());
    cursor.setHours(cursor.getHours() + intervalHours);
  }
  return marks;
}

// RampType values (the same for every pump).
const RAMP_TYPES = ["Sinusoidal", "Logarithmic", "Linear"];

// The reverse of periodForFlags(), with ACTIVE set.
const PERIOD_FLAGS: Record<Period, number> = { day: 1, night: 3, sunrise: 7, sunset: 11 };

// Like the app, the mode list offers Sync, Anti-Sync and EcoSmart Back
// instead of a PhaseShift value: Anti-Sync is Sync with PhaseShift 180.
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

// Mode dropdown options: Sync becomes Sync and Anti-Sync.
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

  // Slider value shown until the debounced intensity write completes, so the
  // slider doesn't jump back to the old entity value in between.
  @state() private _pendingIntensityPercent?: number;
  @state() private _pendingLunarToggle = false;
  @state() private _lunarToggleError?: string;

  @state() private _channelHistoryByEntity: Record<string, HistoryPoint[]> = {};
  @state() private _historyLoading = false;

  // Hover position as a fraction (0-1) of the chart width. Not part of the
  // chart memoization key (see _renderChartHoverOverlay()).
  @state() private _chartHoverFraction?: number;

  @state() private _view: "glance" | "edit" = "glance";
  @state() private _scheduleLoading = false;
  @state() private _scheduleError?: string;
  @state() private _schedulePoints: ScheduleEntry[] = [];

  // State of Save schedule to device (point edits only change
  // _schedulePoints). The success message clears itself; an error stays until
  // the next save.
  @state() private _savingSchedule = false;
  @state() private _saveScheduleError?: string;
  @state() private _saveScheduleSucceeded = false;

  // .mob export/import. Import only replaces _schedulePoints; the device is
  // written by Save schedule to device.
  @state() private _exportingMob = false;
  @state() private _importingMob = false;
  @state() private _mobError?: string;

  // Index of the point being edited (null if none), and the copy being edited
  // (_schedulePoints only changes on Save).
  @state() private _editingIndex: number | null = null;
  @state() private _workingPoint?: ScheduleEntry;

  // True when the point being edited was just created by Add point, so
  // cancelling removes it instead of keeping it.
  @state() private _isNewPoint = false;

  // Pumps of the same tank other than this one, for the parent pump picker.
  @state() private _otherPumps: ScheduleGroupMember[] = [];

  private _intensityDebounceHandle?: ReturnType<typeof setTimeout>;

  // device_id the group was last resolved for; hass changes on every state
  // change, so resolution only runs again when device_id changes.
  private _resolvedFor?: string;

  // Periodic history refresh (history isn't pushed like entity states).
  // Stopped when the card is disconnected.
  private _historyRefreshHandle?: ReturnType<typeof setInterval>;
  private static readonly HISTORY_REFRESH_INTERVAL_MS = 5 * 60 * 1000;

  // Counts the active scene's remaining time down between polls.
  private _countdown = new CountdownTicker(this);

  public disconnectedCallback(): void {
    super.disconnectedCallback();
    clearInterval(this._historyRefreshHandle);
    this._countdown.stop();
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
    // Sections view size: the light chart needs about 12x7, the pump view
    // 6x4. Min/max bounds let the layout shrink on narrow screens.
    if (this._group?.kind === "light") {
      return { columns: 12, rows: 7, min_columns: 4, max_columns: 12, min_rows: 5, max_rows: 10 };
    }
    return { columns: 6, rows: 4, min_columns: 3, max_columns: 12, min_rows: 3, max_rows: 6 };
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

      // Every other pump of the tank, from the same response.
      this._otherPumps = response.groups
        .filter((g) => g.kind === "pump")
        .flatMap((g) => g.members)
        .filter((m) => m.device_id !== deviceId);

      clearInterval(this._historyRefreshHandle);
      if (group.kind === "light") {
        // Not awaited: only the chart depends on history.
        this._fetchChannelHistory(group);
        this._historyRefreshHandle = setInterval(
          () => this._fetchChannelHistory(group),
          MobiusScheduleCard.HISTORY_REFRESH_INTERVAL_MS,
        );
      }
    } catch (err) {
      this._error = errorMessage(err);
      this._group = undefined;
    } finally {
      this._loading = false;
    }
  }

  // History of every member's channel sensors, keyed by entity_id, so the
  // chart can switch to another member (see _pickAvailableLightMember())
  // without fetching again.
  private async _fetchChannelHistory(group: ScheduleGroup): Promise<void> {
    const entityIds = group.members
      .flatMap((m) => Object.values(m.channel_entity_ids ?? {}))
      .filter((id): id is string => !!id);
    if (entityIds.length === 0) return;

    this._historyLoading = true;
    try {
      const response = await this.hass.callWS<Record<string, CompressedStateEntry[]>>({
        type: "history/history_during_period",
        start_time: new Date(chartWindowStartMs()).toISOString(),
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
      // The chart shows its own error state; the rest of the card still works.
      this._channelHistoryByEntity = {};
    } finally {
      this._historyLoading = false;
    }
  }

  // Ends point editing. A point created by Add point and not saved
  // (discardNew) is removed again.
  private _finishEditing(discardNew: boolean): void {
    if (discardNew && this._isNewPoint && this._editingIndex != null) {
      this._schedulePoints = this._schedulePoints.filter((_, i) => i !== this._editingIndex);
    }
    this._editingIndex = null;
    this._workingPoint = undefined;
    this._isNewPoint = false;
  }

  private _closeEdit(): void {
    this._finishEditing(true);
    this._view = "glance";
    this._saveScheduleError = undefined;
    this._saveScheduleSucceeded = false;
  }

  private _startEditingPoint(index: number): void {
    this._editingIndex = index;
    this._isNewPoint = false;
    // Copy params/channels so edits don't reach _schedulePoints before
    // Save.
    const point = this._schedulePoints[index];
    this._workingPoint = isPumpEntry(point)
      ? { ...point, params: { ...point.params } }
      : { ...point, channels: { ...point.channels } };
  }

  private _cancelEditingPoint(): void {
    this._finishEditing(true);
  }

  private _saveEditingPoint(): void {
    if (this._editingIndex == null || !this._workingPoint) return;
    const points = [...this._schedulePoints];
    points[this._editingIndex] = this._workingPoint;
    this._schedulePoints = points;
    this._finishEditing(false);
  }

  // Removes the point being edited (locally; the device is only written
  // by Save schedule to device).
  private _deleteEditingPoint(): void {
    if (this._editingIndex == null) return;
    this._schedulePoints = this._schedulePoints.filter((_, i) => i !== this._editingIndex);
    this._finishEditing(false);
  }

  private _updateWorkingPointTime(minutes: number): void {
    if (!this._workingPoint) return;
    this._workingPoint = { ...this._workingPoint, time_minutes: minutes };
  }

  private _updateWorkingPointPeriod(period: Period): void {
    if (!this._workingPoint) return;
    this._workingPoint = { ...this._workingPoint, flags: PERIOD_FLAGS[period] };
  }

  // Pump edit form only.
  private _updateWorkingPumpMode(displayMode: DisplayMode): void {
    const working = this._workingPoint as PumpScheduleEntry | undefined;
    if (!working || !this._group) return;

    // Anti-Sync is Sync with PhaseShift 180; Sync and EcoSmartBack otherwise
    // use 0 (PhaseShift isn't editable).
    const realMode = displayMode === "AntiSync" ? "Sync" : displayMode;
    const presetPhaseShift =
      displayMode === "AntiSync" ? 180 : realMode === "Sync" || realMode === "EcoSmartBack" ? 0 : undefined;

    const paramNames = this._group.mode_params?.[realMode] ?? [];
    // Parameters shared with the previous mode keep their values; new ones
    // get a default (python-mobius rejects missing parameters).
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
      this._scheduleError = errorMessage(err);
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
      // Points are sent in the format read_schedule_group returned (the service
      // converts ParentSerial back). Pump points are saved with flags = ACTIVE
      // only: pumps don't use the day/night/sunrise/sunset flags.
      const points = this._schedulePoints.map((p) => (isPumpEntry(p) ? { ...p, flags: 1 } : p));
      await this.hass.callService("mobius", "write_schedule_group", {
        device_id: this._config!.device_id,
        points,
      });
      this._saveScheduleSucceeded = true;
      setTimeout(() => {
        this._saveScheduleSucceeded = false;
      }, 4000);
    } catch (err) {
      this._saveScheduleError = errorMessage(err);
    } finally {
      this._savingSchedule = false;
    }
  }

  private async _exportMob(): Promise<void> {
    this._exportingMob = true;
    this._mobError = undefined;
    try {
      // The file content is built by python-mobius.
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
      this._mobError = errorMessage(err);
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

    // accept=".mob" is only a picker hint, so the extension is checked too.
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
      // A .mob file holds a whole schedule. Nothing is written to the device
      // until Save schedule to device.
      this._schedulePoints = response.points;
      this._finishEditing(false);
    } catch (err) {
      this._mobError = errorMessage(err);
    } finally {
      this._importingMob = false;
    }
  }

  // The scene select entity if a scene is active, read live (the group's
  // active_scene is only a snapshot from resolution).
  private _activeSceneState(group?: ScheduleGroup) {
    const stateObj = group?.scene_entity_id ? this.hass.states[group.scene_entity_id] : undefined;
    return stateObj && stateObj.state !== "None" && stateObj.state !== "unavailable" ? stateObj : undefined;
  }

  private _renderSceneBanner() {
    const stateObj = this._activeSceneState(this._group);
    this._countdown.sync(!!stateObj?.attributes.ends_at);
    if (!stateObj) return nothing;

    const duration = sceneRemainingSeconds(stateObj.attributes);
    return html`
      <div class="scene-banner">
        <ha-icon icon="mdi:auto-mode"></ha-icon>
        <span>
          <strong>${stateObj.state}</strong> ${localize("schedule_card.scene_running_instead")}
          ${
            duration != null && duration > 0
              ? html`(${formatDuration(duration)} ${localize("schedule_card.remaining_suffix")})`
              : nothing
          }
        </span>
      </div>
    `;
  }

  // Opens Home Assistant's more-info dialog for an entity.
  private _showMoreInfo(entityId: string): void {
    this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId }, bubbles: true, composed: true }));
  }

  // The text for "currently running": the active scene, else the pump mode
  // (with its parent pump for Sync modes).
  private _currentModeText(group: ScheduleGroup, modeState?: { state: string; attributes: Record<string, unknown> }) {
    const sceneState = this._activeSceneState(group);
    if (sceneState) return sceneState.state;
    if (!modeState) return undefined;

    // CurrentPumpModeSensor's attributes are the mode parameters themselves.
    const params = modeState.attributes;
    const label = displayModeLabel(displayModeFor(modeState.state, params.PhaseShift));
    const parentSerial = params.ParentSerial as string | null | undefined;
    if (parentSerial) {
      const parent = this._otherPumps.find((p) => p.serial === parentSerial);
      if (parent) return `${label} (${parent.name})`;
    }
    return label;
  }

  // A value with its unit that opens the entity's more-info dialog.
  private _renderReading(entityId: string, value: string, unit: unknown) {
    return html`
      <div
        class="reading reading-clickable"
        role="button"
        tabindex="0"
        @click=${() => this._showMoreInfo(entityId)}
        @keydown=${(e: KeyboardEvent) => {
          if (e.key === "Enter" || e.key === " ") this._showMoreInfo(entityId);
        }}
      >
        <span class="reading-value">${value}</span>
        <span class="reading-unit">${unit}</span>
      </div>
    `;
  }

  private _renderPumpGlance() {
    const group = this._group!;
    const member = group.members[0];
    const flowState = member.flow_entity_id ? this.hass.states[member.flow_entity_id] : undefined;
    const speedState = member.speed_entity_id ? this.hass.states[member.speed_entity_id] : undefined;
    const modeState = member.mode_entity_id ? this.hass.states[member.mode_entity_id] : undefined;

    const flowAvailable = isAvailable(flowState);
    const speedAvailable = isAvailable(speedState);

    return html`
      <ha-card>
        <div class="header">
          <div>
            <div class="title">${localize("schedule_card.pump_title")}</div>
            <div class="subtitle">${member.name}</div>
          </div>
        </div>
        ${this._renderSceneBanner()}
        ${
          flowAvailable
            ? // The unit comes from the entity, which reflects a per-entity
              // unit override (the device reports GPH).
              this._renderReading(
                member.flow_entity_id!,
                roundedState(flowState!.state),
                flowState!.attributes.unit_of_measurement,
              )
            : speedAvailable
              ? this._renderReading(
                  member.speed_entity_id!,
                  `${roundedState(speedState!.state)}%`,
                  localize("schedule_card.speed_not_reliable"),
                )
              : html`<div class="reading-missing">${localize("schedule_card.no_flow_data")}</div>`
        }
        ${
          modeState
            ? html`
                <div class="current-mode">
                  ${localize("schedule_card.currently_running")}
                  <strong>${this._currentModeText(group, modeState)}</strong>
                </div>
              `
            : nothing
        }
        <button class="edit-button" @click=${() => this._openEdit()}>${localize("schedule_card.edit_schedule")}</button>
      </ha-card>
    `;
  }

  // The first available member (members are sorted by serial) and the names
  // of the unavailable ones. Availability is judged from one channel sensor
  // per member, since all of a device's sensors share one coordinator.
  private _pickAvailableLightMember(): { member?: ScheduleGroupMember; unavailableNames: string[] } {
    const group = this._group!;
    const unavailableNames: string[] = [];
    let picked: ScheduleGroupMember | undefined;

    for (const member of group.members) {
      const entityIds = member.channel_entity_ids ? Object.values(member.channel_entity_ids) : [];
      const representativeId = entityIds.find((id) => id != null);
      const stateObj = representativeId ? this.hass.states[representativeId] : undefined;
      const available = isAvailable(stateObj);

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

  // Memoized chart: hass changes on every state change in Home Assistant, so
  // the chart is only rebuilt when the member, the history or the language
  // changes.
  private _lastChartKey?: string;
  private _lastChartResult?: TemplateResult;

  private _handleChartMouseMove(e: MouseEvent): void {
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    this._chartHoverFraction = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  }

  private _handleChartMouseLeave(): void {
    this._chartHoverFraction = undefined;
  }

  // Hover line and tooltip, rendered separately from the memoized chart
  // because they change on every mouse move.
  private _renderChartHoverOverlay(sourceMember: ScheduleGroupMember) {
    if (this._chartHoverFraction === undefined) return nothing;

    const entries = channelEntities(sourceMember);
    const hoverTimeMs = chartWindowStartMs() + this._chartHoverFraction * 24 * 60 * 60 * 1000;

    // The nearest recorded value per channel (history is a step function).
    const readings = entries
      .map(([channelName, entityId]) => {
        const points = this._channelHistoryByEntity[entityId];
        if (!points || points.length === 0) return null;
        const nearest = points.reduce((best, p) =>
          Math.abs(p.t - hoverTimeMs) < Math.abs(best.t - hoverTimeMs) ? p : best,
        );
        return { channelName, value: Math.round(nearest.v) };
      })
      .filter((r): r is { channelName: string; value: number } => r !== null);

    if (readings.length === 0) return nothing;

    const hoverTimeLabel = formatClock(new Date(hoverTimeMs), this.hass?.locale);

    return html`
      <div class="chart-hover-line" style="left: ${this._chartHoverFraction * 100}%"></div>
      <div class="chart-hover-time" style="left: ${this._chartHoverFraction * 100}%">${hoverTimeLabel}</div>
      <div class="chart-tooltip" style="left: ${this._chartHoverFraction * 100}%">
        ${readings.map(
          (r) => html`
            <div class="chart-tooltip-row">
              <span class="chart-tooltip-swatch" style="background: ${channelColor(r.channelName)}"></span>
              <span class="chart-tooltip-name">${r.channelName}</span>
              <span class="chart-tooltip-value">${r.value}%</span>
            </div>
          `,
        )}
      </div>
    `;
  }

  private _renderChartLegend(sourceMember: ScheduleGroupMember) {
    const channelNames = channelEntities(sourceMember).map(([name]) => name);
    if (channelNames.length === 0) return nothing;
    return html`
      <div class="chart-legend">
        ${channelNames.map(
          (name) => html`
            <span class="chart-legend-item">
              <span class="chart-legend-swatch" style="background: ${channelColor(name)}"></span>
              ${name}
            </span>
          `,
        )}
      </div>
    `;
  }

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
    const entries = channelEntities(sourceMember);
    if (entries.length === 0) {
      return html`<div class="chart-status">${localize("schedule_card.no_channel_data")}</div>`;
    }

    const windowStartMs = chartWindowStartMs();
    const dayMs = 24 * 60 * 60 * 1000;

    const toX = (t: number) => ((t - windowStartMs) / dayMs) * CHART_WIDTH;
    const toY = (v: number) => CHART_HEIGHT - (Math.max(0, Math.min(100, v)) / 100) * CHART_HEIGHT;

    const nowMs = Date.now();
    const lines = entries
      .map(([channelName, entityId]) => {
        const points = this._channelHistoryByEntity[entityId];
        if (!points || points.length === 0) return nothing;
        // A state that doesn't change produces no history, so the line is
        // extended to now with the last value (like Home Assistant's charts).
        const last = points[points.length - 1];
        const extended = nowMs > last.t ? [...points, { t: nowMs, v: last.v }] : points;
        const path = extended.map((p) => `${toX(p.t).toFixed(1)},${toY(p.v).toFixed(1)}`).join(" ");
        return svg`<polyline points=${path} fill="none" stroke=${channelColor(channelName)} stroke-width="2" />`;
      })
      .filter((l) => l !== nothing);

    if (lines.length === 0) {
      return html`<div class="chart-status">${localize("schedule_card.no_history_yet")}</div>`;
    }

    const lang = this.hass?.locale;
    const hourMarks = niceHourMarks(windowStartMs, nowMs, 6);

    return html`
      <svg class="chart" viewBox="0 0 ${CHART_WIDTH} ${CHART_HEIGHT}" preserveAspectRatio="none">
        ${[0, 50, 100].map(
          (percent) => svg`
            <line x1="0" x2=${CHART_WIDTH} y1=${toY(percent)} y2=${toY(percent)} class="chart-gridline-h" />
          `,
        )}
        ${hourMarks.map(
          (t) => svg`
            <line x1=${toX(t)} x2=${toX(t)} y1="0" y2=${CHART_HEIGHT} class="chart-gridline" />
          `,
        )}
        ${lines}
      </svg>
      <div class="chart-hour-labels">
        ${hourMarks.map(
          (t) =>
            html`<span class="chart-hour-label" style="left: ${(toX(t) / CHART_WIDTH) * 100}%"
              >${formatClock(new Date(t), lang)}</span
            >`,
        )}
      </div>
    `;
  }

  // Lunar phase state of a light group: the lunar switches of every member
  // (a setting is applied to the whole group, like the schedule), whether
  // the shown member has it on, and its current moon phase icon.
  private _lunarState() {
    const { member } = this._pickAvailableLightMember();
    const entityIds = (this._group?.members ?? [])
      .map((m) => m.lunar_switch_entity_id)
      .filter((id): id is string => !!id);
    const switchId = member?.lunar_switch_entity_id;
    const intensityId = member?.schedule_intensity_entity_id;
    const phaseIcon: string | undefined = intensityId
      ? this.hass.states[intensityId]?.attributes.moon_phase_icon
      : undefined;
    return {
      entityIds,
      available: !!switchId,
      on: !!switchId && this.hass.states[switchId]?.state === "on",
      phaseIcon,
    };
  }

  // Turns lunar phases on or off on every light of the group.
  private async _setLunar(entityIds: string[], enable: boolean): Promise<void> {
    if (this._pendingLunarToggle || entityIds.length === 0) return;
    this._pendingLunarToggle = true;
    this._lunarToggleError = undefined;
    try {
      await this.hass.callService("switch", enable ? "turn_on" : "turn_off", { entity_id: entityIds });
    } catch (err) {
      this._lunarToggleError = errorMessage(err);
    } finally {
      this._pendingLunarToggle = false;
    }
  }

  private _renderLightGlance() {
    const group = this._group!;
    const { member: sourceMember, unavailableNames } = this._pickAvailableLightMember();

    const intensityEntityId = sourceMember?.schedule_intensity_entity_id;
    const intensityState = intensityEntityId ? this.hass.states[intensityEntityId] : undefined;
    const liveIntensityPercent = intensityState ? Math.round(Number(intensityState.state)) : undefined;
    const displayedIntensityPercent = this._pendingIntensityPercent ?? liveIntensityPercent;

    const lunar = this._lunarState();
    return html`
      <ha-card>
        <div class="header">
          <div>
            <div class="title">${localize("schedule_card.light_title")}</div>
            <div class="subtitle">${group.members.map((m) => m.name).join(" + ")}</div>
          </div>
          ${
            lunar.on
              ? html`<span
                  class="moon-indicator"
                  title=${
                    lunar.phaseIcon
                      ? moonPhaseName(lunar.phaseIcon, this.hass)
                      : localize("schedule_card.lunar_phases_active")
                  }
                >
                  <ha-icon icon=${lunar.phaseIcon ?? "mdi:moon-waning-crescent"}></ha-icon>
                </span>`
              : nothing
          }
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
            ? html`
                <div class="chart-wrapper">
                  <div class="chart-y-axis">
                    <span>100%</span>
                    <span>50%</span>
                    <span>0%</span>
                  </div>
                  <div
                    class="chart-container"
                    @mousemove=${this._handleChartMouseMove}
                    @mouseleave=${this._handleChartMouseLeave}
                  >
                    ${this._renderChannelChart(sourceMember)} ${this._renderChartHoverOverlay(sourceMember)}
                  </div>
                </div>
                ${this._renderChartLegend(sourceMember)}
              `
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

  // Lunar phases on/off for the whole group (edit view header). Only shown
  // when the light supports lunar phases.
  private _renderLunarToggle() {
    const lunar = this._lunarState();
    if (!lunar.available) return nothing;
    const label = localize(lunar.on ? "schedule_card.lunar_disable" : "schedule_card.lunar_enable");
    return html`<button
      class="header-icon-button moon-toggle ${lunar.on ? "on" : ""}"
      ?disabled=${this._pendingLunarToggle}
      title=${label}
      aria-label=${label}
      @click=${() => this._setLunar(lunar.entityIds, !lunar.on)}
    >
      <ha-icon
        icon=${this._pendingLunarToggle ? "mdi:loading" : lunar.on ? (lunar.phaseIcon ?? "mdi:moon-waning-crescent") : "mdi:moon-new"}
        class=${this._pendingLunarToggle ? "spin" : ""}
      ></ha-icon>
    </button>`;
  }

  // Card, header (back, title, lunar toggle, .mob import/export) and content
  // of the edit view.
  private _renderEditShell(content: unknown) {
    return html`
      <ha-card>
        <div class="edit-header">
          <button
            class="back-button"
            title=${localize("schedule_card.back")}
            aria-label=${localize("schedule_card.back")}
            @click=${() => this._closeEdit()}
          >
            <ha-icon icon="mdi:arrow-left"></ha-icon>
          </button>
          <div class="title">${localize("schedule_card.edit_schedule")}</div>
          ${this._group?.kind === "light" ? this._renderLunarToggle() : nothing}
          <button
            class="header-icon-button"
            ?disabled=${this._importingMob || this._editingIndex != null}
            title=${localize("schedule_card.load_mob")}
            aria-label=${localize("schedule_card.load_mob")}
            @click=${() => this._triggerMobFilePicker()}
          >
            <ha-icon
              icon=${this._importingMob ? "mdi:loading" : "mdi:upload"}
              class=${this._importingMob ? "spin" : ""}
            ></ha-icon>
          </button>
          <button
            class="header-icon-button"
            ?disabled=${this._exportingMob || this._editingIndex != null}
            title=${localize("schedule_card.download_mob")}
            aria-label=${localize("schedule_card.download_mob")}
            @click=${() => this._exportMob()}
          >
            <ha-icon
              icon=${this._exportingMob ? "mdi:loading" : "mdi:download"}
              class=${this._exportingMob ? "spin" : ""}
            ></ha-icon>
          </button>
          <input
            type="file"
            class="mob-file-input"
            accept=".mob"
            style="display: none"
            @change=${(e: Event) => this._handleMobFileSelected(e)}
          />
        </div>
        ${this._mobError ? html`<div class="save-schedule-error">${this._mobError}</div>` : nothing}
        ${this._lunarToggleError ? html`<div class="save-schedule-error">${this._lunarToggleError}</div>` : nothing}
        ${content}
      </ha-card>
    `;
  }

  // Save schedule to device, its status messages, and the note about the
  // Mobius app keeping its own copy of the schedule.
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
      <div class="app-cache-note">
        <ha-icon icon="mdi:information-outline"></ha-icon>
        <span>${localize("schedule_card.app_cache_note")}</span>
      </div>
    `;
  }

  // The edit view of either kind; only the point edit form differs.
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
      ${this._renderSaveScheduleControls()}
    `);
  }

  private _renderPumpEditView() {
    return this._renderScheduleEditView(
      () => this._renderPumpPointEditForm(),
      () => this._addPumpPoint(),
    );
  }

  // One point in the list: time plus the mode (pump), or channel bars and
  // period (light).
  private _renderPointRow(point: ScheduleEntry, index: number) {
    const period = periodForFlags(point.flags);
    return html`
      <button class="point-row" @click=${() => this._startEditingPoint(index)}>
        <span class="point-time">${formatMinutes(point.time_minutes)}</span>
        ${
          isPumpEntry(point)
            ? html`<span class="point-mode">${point.mode}</span>`
            : html`${this._renderLightChannelBars(point)}
                <span class="point-period period-${period}">${periodLabel(period)}</span>`
        }
      </button>
    `;
  }

  // One small bar per channel, sized and shaded by its intensity.
  private _renderLightChannelBars(point: LightScheduleEntry) {
    return html`
      <span class="point-channel-bars">
        ${Object.entries(point.channels).map(([channel, value]) => {
          const pct = Math.max(0, Math.min(100, value));
          return html`
            <span
              class="point-channel-bar"
              title="${channel}: ${Math.round(pct)}%"
              style="height: ${Math.max(3, (pct / 100) * 18)}px; background: ${channelColor(
                channel,
              )}; opacity: ${0.5 + 0.5 * (pct / 100)}"
            ></span>
          `;
        })}
      </span>
    `;
  }

  // Time input (hours, minutes and, for 12-hour format, AM/PM). Pumps have
  // no period, so it is used alone for pump points.
  private _renderTimeField(point: ScheduleEntry) {
    const hours24 = Math.floor(point.time_minutes / 60);
    const minutes = point.time_minutes % 60;
    const useAmPm = shouldUseAmPm(this.hass);
    const hourDisplay = useAmPm ? (hours24 % 12 === 0 ? 12 : hours24 % 12) : hours24;
    const amPm = hours24 < 12 ? "AM" : "PM";

    const onTimePartChanged = (e: Event) => {
      const container = (e.target as HTMLElement).closest(".time-fields") as HTMLElement;
      const hourInput = container.querySelector<HTMLInputElement>(".time-hour")!;
      const minInput = container.querySelector<HTMLInputElement>(".time-minute")!;
      const ampmSelect = container.querySelector<HTMLSelectElement>(".time-ampm");
      let h = Number(hourInput.value);
      const m = Number(minInput.value);
      if (useAmPm) {
        // 12 AM -> 0, 12 PM -> 12.
        h = h % 12;
        if (ampmSelect?.value === "PM") h += 12;
      }
      this._updateWorkingPointTime(h * 60 + m);
    };

    return html`
      <label>
        ${localize("schedule_card.time")}
        <span class="time-fields">
          <input
            class="time-hour"
            type="number"
            min=${useAmPm ? 1 : 0}
            max=${useAmPm ? 12 : 23}
            .value=${String(hourDisplay)}
            @change=${onTimePartChanged}
          />
          <span class="time-colon">:</span>
          <input
            class="time-minute"
            type="number"
            min="0"
            max="59"
            .value=${String(minutes).padStart(2, "0")}
            @change=${onTimePartChanged}
          />
          ${
            useAmPm
              ? html`<select class="time-ampm" .value=${amPm} @change=${onTimePartChanged}>
                  <option value="AM" ?selected=${amPm === "AM"}>AM</option>
                  <option value="PM" ?selected=${amPm === "PM"}>PM</option>
                </select>`
              : nothing
          }
        </span>
      </label>
    `;
  }

  // Time row of a pump point.
  private _renderTimeOnlyField(point: ScheduleEntry) {
    return html` <div class="edit-row">${this._renderTimeField(point)}</div> `;
  }

  // Time and period row of a light point.
  private _renderTimeAndPeriodFields(point: ScheduleEntry) {
    const period = periodForFlags(point.flags);
    return html`
      <div class="edit-row">
        ${this._renderTimeField(point)}
        <label>
          ${localize("schedule_card.period")}
          <select
            class="period-select"
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

  // Cancel/Delete/Save of the point being edited.
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

  // Pump points: mode dropdown and parameter fields.
  private _addPumpPoint(): void {
    const firstMode = this._group?.modes?.[0];
    if (!firstMode) return;
    const newPoint: PumpScheduleEntry = { time_minutes: 0, flags: 1, mode: firstMode, params: {} };
    this._schedulePoints = [...this._schedulePoints, newPoint];
    this._startEditingPoint(this._schedulePoints.length - 1);
    this._isNewPoint = true;
    // Fills in the parameters of the first mode.
    this._updateWorkingPumpMode(displayModeFor(firstMode, undefined));
  }

  private _renderPumpPointEditForm() {
    const point = this._workingPoint as PumpScheduleEntry;
    const modes = this._group?.modes ?? [];
    const displayModes = displayModeOptions(modes);
    const currentDisplayMode = displayModeFor(point.mode, point.params.PhaseShift);
    // PhaseShift follows from the chosen mode and isn't shown.
    const paramNames = (this._group?.mode_params?.[point.mode] ?? []).filter((name) => name !== "PhaseShift");

    return html`
      <div class="point-edit-form">
        ${this._renderTimeOnlyField(point)}
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
    if (name === "Variance") {
      // Variance is shown as the app's four levels (0, <400, <700, else); each
      // choice writes a representative value.
      const VARIANCE_OPTIONS: [string, number][] = [
        [localize("schedule_card.variance_none"), 0],
        [localize("schedule_card.variance_low"), 200],
        [localize("schedule_card.variance_medium"), 550],
        [localize("schedule_card.variance_high"), 850],
      ];
      const currentLabel = (() => {
        const n = Number(value ?? 0);
        if (n === 0) return VARIANCE_OPTIONS[0][0];
        if (n < 400) return VARIANCE_OPTIONS[1][0];
        if (n < 700) return VARIANCE_OPTIONS[2][0];
        return VARIANCE_OPTIONS[3][0];
      })();
      return html`
        <label class="param-label">
          ${name}
          <select
            .value=${currentLabel}
            @change=${(e: Event) => {
              const picked = VARIANCE_OPTIONS.find((o) => o[0] === (e.target as HTMLSelectElement).value);
              if (picked) this._updateWorkingPumpParam(name, picked[1]);
            }}
          >
            ${VARIANCE_OPTIONS.map(
              ([label]) => html`<option value=${label} ?selected=${label === currentLabel}>${label}</option>`,
            )}
          </select>
        </label>
      `;
    }
    // A negative MaxSpeed/MinSpeed reverses rotation, only on pumps with
    // supports_reverse (AlpacaV1). Other pumps don't accept negative values.
    const isSpeedParam = name === "MaxSpeed" || name === "MinSpeed";
    if (isSpeedParam) {
      const supportsReverse = this._group?.members?.[0]?.supports_reverse === true;
      // Shown as whole percent (the raw value is tenths of a percent); the sign
      // is kept.
      const raw = Number(value ?? 0);
      const displayPercent = Math.sign(raw) * Math.round(Math.abs(raw) / 10);
      return html`
        <label class="param-label">
          ${name} (%)
          <input
            type="number"
            min=${supportsReverse ? nothing : 0}
            .value=${String(displayPercent)}
            title=${supportsReverse ? localize("schedule_card.reverse_hint") : nothing}
            @change=${(e: Event) => {
              let percent = Number((e.target as HTMLInputElement).value);
              // The device would ignore a negative value.
              if (!supportsReverse && percent < 0) percent = Math.abs(percent);
              this._updateWorkingPumpParam(name, Math.sign(percent) * Math.round(Math.abs(percent) * 10));
            }}
          />
          ${supportsReverse ? html`<span class="field-hint">${localize("schedule_card.reverse_hint")}</span>` : nothing}
        </label>
      `;
    }
    return html`
      <label class="param-label">
        ${name}
        <input
          type="number"
          .value=${String(value ?? 0)}
          @change=${(e: Event) => this._updateWorkingPumpParam(name, Number((e.target as HTMLInputElement).value))}
        />
      </label>
    `;
  }

  private _renderLightEditView() {
    return this._renderScheduleEditView(
      () => this._renderLightPointEditForm(),
      () => this._addLightPoint(),
    );
  }

  // Light points: one intensity slider per channel.
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
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 8px;
    }
    .moon-indicator {
      display: flex;
      flex-shrink: 0;
      padding: 4px;
      color: var(--primary-color);
    }
    .moon-indicator ha-icon {
      --mdc-icon-size: 22px;
    }
    .moon-toggle {
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
      border: none;
      background: none;
      cursor: pointer;
      padding: 4px;
      color: var(--secondary-text-color);
      border-radius: 50%;
    }
    .moon-toggle:hover {
      background: rgba(0, 0, 0, 0.05);
    }
    .moon-toggle:disabled {
      cursor: default;
      opacity: 0.6;
    }
    .moon-toggle.on {
      color: var(--primary-color);
    }
    .moon-toggle ha-icon {
      --mdc-icon-size: 22px;
    }
    .moon-toggle ha-icon.spin {
      animation: mobius-spin 1s linear infinite;
    }
    .activation-error {
      margin: 4px 0 10px;
      padding: 8px 10px;
      border-radius: 8px;
      background: rgba(219, 68, 55, 0.1);
      color: var(--error-color, #db4437);
      font-size: 0.85em;
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
    .reading-clickable {
      cursor: pointer;
      border-radius: 8px;
      padding: 2px 6px;
      margin-left: -6px;
    }
    .reading-clickable:hover,
    .reading-clickable:focus-visible {
      background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.08);
      outline: none;
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
      gap: 4px;
      margin-bottom: 14px;
    }
    .edit-header .title {
      flex: 1;
      margin: 0 8px;
    }
    .header-icon-button {
      display: flex;
      align-items: center;
      justify-content: center;
      background: none;
      border: none;
      color: var(--primary-text-color);
      cursor: pointer;
      padding: 6px;
      border-radius: 50%;
    }
    .header-icon-button:last-of-type {
      margin-right: -6px;
    }
    .header-icon-button:hover {
      background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.08);
    }
    .header-icon-button:disabled {
      opacity: 0.5;
      cursor: default;
    }
    .header-icon-button ha-icon {
      --mdc-icon-size: 20px;
    }
    .header-icon-button ha-icon.spin {
      animation: mobius-spin 1s linear infinite;
    }
    @keyframes mobius-spin {
      from {
        transform: rotate(0deg);
      }
      to {
        transform: rotate(360deg);
      }
    }
    .back-button {
      display: flex;
      align-items: center;
      justify-content: center;
      background: none;
      border: none;
      color: var(--primary-color);
      cursor: pointer;
      padding: 6px;
      margin: -6px;
      border-radius: 50%;
    }
    .back-button:hover {
      background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.08);
    }
    .back-button ha-icon {
      --mdc-icon-size: 22px;
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
    .time-fields {
      display: flex;
      align-items: center;
      gap: 4px;
    }
    .time-fields .time-hour,
    .time-fields .time-minute {
      width: 3em;
      text-align: center;
    }
    .time-colon {
      color: var(--secondary-text-color);
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
    .app-cache-note {
      display: flex;
      gap: 6px;
      align-items: flex-start;
      margin: 8px 16px 4px;
      color: var(--secondary-text-color);
      font-size: 0.8em;
      line-height: 1.35;
    }
    .app-cache-note ha-icon {
      --mdc-icon-size: 16px;
      flex-shrink: 0;
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
    .point-channel-bars {
      display: flex;
      align-items: flex-end;
      gap: 3px;
      height: 18px;
      flex: 1;
      margin-left: 8px;
    }
    .point-channel-bar {
      width: 7px;
      border-radius: 2px;
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
    .chart-wrapper {
      display: flex;
      gap: 6px;
    }
    .chart-y-axis {
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      padding: 0 0 18px 0;
      font-size: 11px;
      color: var(--secondary-text-color);
      text-align: right;
      min-width: 30px;
    }
    .chart-container {
      position: relative;
      flex: 1;
      min-width: 0;
    }
    .chart-hover-line {
      position: absolute;
      top: 0;
      bottom: 0;
      width: 1px;
      background: var(--primary-text-color);
      opacity: 0.3;
      pointer-events: none;
      transform: translateX(-50%);
    }
    .chart-hover-time {
      position: absolute;
      bottom: -18px;
      transform: translateX(-50%);
      font-size: 11px;
      font-weight: 500;
      color: var(--primary-text-color);
      background: var(--card-background-color);
      padding: 1px 5px;
      border-radius: 4px;
      border: 1px solid var(--divider-color);
      pointer-events: none;
      white-space: nowrap;
      z-index: 1;
    }
    .chart-tooltip {
      position: absolute;
      bottom: 100%;
      margin-bottom: 6px;
      transform: translateX(-50%);
      background: var(--card-background-color);
      border: 1px solid var(--divider-color);
      border-radius: 8px;
      padding: 6px 8px;
      font-size: 0.78em;
      pointer-events: none;
      box-shadow: 0 2px 6px rgba(0, 0, 0, 0.15);
      white-space: nowrap;
      z-index: 2;
    }
    .chart-tooltip-row {
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .chart-tooltip-swatch {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      flex-shrink: 0;
    }
    .chart-tooltip-name {
      flex: 1;
    }
    .chart-tooltip-value {
      font-variant-numeric: tabular-nums;
      font-weight: 500;
    }
    .chart-legend {
      display: flex;
      flex-wrap: wrap;
      justify-content: center;
      gap: 10px;
      margin: 10px 0 12px;
      font-size: 0.78em;
      color: var(--secondary-text-color);
    }
    .chart-legend-item {
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }
    .chart-legend-swatch {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      display: inline-block;
    }
    .chart-gridline {
      stroke: var(--divider-color);
      stroke-width: 1;
    }
    .chart-gridline-h {
      stroke: var(--divider-color);
      stroke-width: 1;
      opacity: 0.5;
    }
    .chart-hour-labels {
      position: relative;
      height: 16px;
      margin-top: 2px;
    }
    .chart-hour-label {
      position: absolute;
      transform: translateX(-50%);
      font-size: 11px;
      color: var(--secondary-text-color);
      white-space: nowrap;
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
