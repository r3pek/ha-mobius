import { LitElement, html, css, nothing } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import type { HomeAssistant, LovelaceCardConfig } from "custom-card-helpers";
import { localize } from "./localize/localize";

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

    // Real glance/edit rendering for both kinds lands in follow-up
    // work -- this confirms the resolution pipeline itself end to
    // end: device_id -> Tank -> resolve_schedule_groups -> the one
    // matching group, with its real member/channel/mode data.
    return html`
      <ha-card>
        <div class="header">
          <div class="title">${this._group.kind === "light" ? "Light Schedule" : "Pump Schedule"}</div>
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
    }
    .warning {
      padding: 12px 0;
      color: var(--error-color, #db4437);
    }
    .loading {
      padding: 12px 0;
      color: var(--secondary-text-color);
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
