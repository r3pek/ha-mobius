import { LitElement, html, css, nothing } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import type { HomeAssistant, LovelaceCardConfig } from "custom-card-helpers";
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

@customElement("mobius-schedule-card")
export class MobiusScheduleCard extends LitElement {
  @property({ attribute: false }) public hass!: ExtendedHomeAssistant;

  @state() private _config?: MobiusScheduleCardConfig;
  @state() private _group?: ScheduleGroup;
  @state() private _error?: string;
  @state() private _loading = false;

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
    } catch (err) {
      this._error = err instanceof Error ? err.message : String(err);
      this._group = undefined;
    } finally {
      this._loading = false;
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

    // Light glance/edit views, and the pump edit view, land in
    // follow-up work.
    return html`
      <ha-card>
        <div class="header">
          <div class="title">${localize("schedule_card.light_title")}</div>
          <div class="subtitle">${this._group.members.map((m) => m.name).join(" + ")}</div>
        </div>
      </ha-card>
    `;
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
