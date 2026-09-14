import { LitElement, html, css, nothing } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import type { HomeAssistant, LovelaceCardConfig } from "custom-card-helpers";
import { localize } from "./localize/localize";
import { formatDuration } from "./format";

/**
 * mobius-scene-card
 *
 * Presents an existing select.*_scene_selection entity (already
 * shipped, real, working functionality -- options/current_option/
 * select_option()) as a row of tappable tiles instead of a generic
 * dropdown, with the active scene's own remaining time shown via that
 * entity's own duration_remaining_seconds attribute. Tank-wide by
 * nature -- one scene activation affects every device on the tank at
 * once -- so this card's only config is which select entity to show,
 * never a device_id.
 *
 * This card does not call any Mobius-specific backend at all: every
 * capability it needs already exists on the select entity itself, via
 * the same select.select_option service every other HA select entity
 * already supports. That's deliberate -- there's no new
 * Mobius-specific functionality this card introduces, only a nicer
 * presentation of what's already there.
 */

interface MobiusSceneCardConfig extends LovelaceCardConfig {
  entity: string;
}

const NONE_OPTION = "None"; // must match select.py's own SceneSelectionSelect.NONE_OPTION exactly

// No icon is ever stored anywhere for a scene -- it's purely a name,
// both on the wire and in the app. This is a purely cosmetic,
// best-effort guess at a fitting icon for a few common factory scene
// names; anything unmatched (including any scene a person names
// themselves) gets one consistent, clearly-generic placeholder rather
// than a different guess per name, which would wrongly suggest a real
// per-scene icon exists to guess from.
const SCENE_ICON_GUESSES: Record<string, string> = {
  sunrise: "mdi:weather-sunset-up",
  sunset: "mdi:weather-sunset-down",
  feed: "mdi:food-drumstick",
  feeding: "mdi:food-drumstick",
  storm: "mdi:weather-lightning-rainy",
  moon: "mdi:weather-night",
  moonlit: "mdi:weather-night",
  clean: "mdi:broom",
  "water change": "mdi:water-sync",
};
const PLACEHOLDER_ICON = "mdi:bookmark-outline";

function iconFor(name: string): string {
  const lower = name.toLowerCase();
  for (const [needle, icon] of Object.entries(SCENE_ICON_GUESSES)) {
    if (lower.includes(needle)) return icon;
  }
  return PLACEHOLDER_ICON;
}

@customElement("mobius-scene-card")
export class MobiusSceneCard extends LitElement {
  @property({ attribute: false }) public hass!: HomeAssistant;

  @state() private _config?: MobiusSceneCardConfig;
  @state() private _pendingOption?: string;
  @state() private _activationError?: string;

  public setConfig(config: MobiusSceneCardConfig): void {
    if (!config || !config.entity) {
      throw new Error('mobius-scene-card: "entity" is required (a select.*_scene_selection entity)');
    }
    this._config = config;
  }

  public getCardSize(): number {
    return 3;
  }

  // Sections view (newer, grid-based dashboards) -- masonry view uses
  // getCardSize() above instead; both are defined since either
  // layout might be in use.
  public getGridOptions() {
    return { rows: 3, columns: 6, min_rows: 3 };
  }

  public static getStubConfig(_hass: HomeAssistant, entities: string[]): Partial<MobiusSceneCardConfig> {
    const guess = (entities || []).find((e) => e.startsWith("select.") && e.includes("scene"));
    return { entity: guess || "" };
  }

  // The built-in form editor (see Home Assistant's own developer docs
  // on custom-card configuration) -- the right fit for a card with
  // exactly one config field, rather than a hand-built editor
  // element. domain: "select" on the entity selector means the
  // picker only offers select.* entities in the first place, not
  // free-text entry a person could get wrong.
  public static getConfigForm() {
    return {
      schema: [{ name: "entity", required: true, selector: { entity: { domain: "select" } } }] as const,
      computeLabel: (schema: { name: string }) => (schema.name === "entity" ? localize("editor.entity") : undefined),
      computeHelper: (schema: { name: string }) =>
        schema.name === "entity" ? localize("editor.entity_helper") : undefined,
    };
  }

  private get _stateObj() {
    return this.hass && this._config ? this.hass.states[this._config.entity] : undefined;
  }

  private async _selectOption(option: string): Promise<void> {
    const stateObj = this._stateObj;
    if (!stateObj || this._pendingOption != null) return;
    this._pendingOption = option;
    this._activationError = undefined;
    try {
      await this.hass.callService("select", "select_option", {
        entity_id: stateObj.entity_id,
        option,
      });
    } catch (err) {
      this._activationError = err instanceof Error ? err.message : String(err);
    } finally {
      this._pendingOption = undefined;
    }
  }

  private _renderTile(name: string, isActive: boolean) {
    const isPending = name === this._pendingOption;
    const icon = name === NONE_OPTION ? "mdi:calendar-clock" : iconFor(name);
    const label =
      name === NONE_OPTION ? localize("scene_card.normal_schedule_tile", this.hass?.locale?.language) : name;
    return html`
      <button
        class="tile ${isActive ? "active" : ""}"
        ?disabled=${this._pendingOption != null}
        @click=${() => this._selectOption(name)}
        title=${label}
      >
        ${isActive && !isPending ? html`<ha-icon class="check" icon="mdi:check-circle"></ha-icon>` : nothing}
        <ha-icon icon=${isPending ? "mdi:loading" : icon} class=${isPending ? "spin" : ""}></ha-icon>
        <span>${label}</span>
      </button>
    `;
  }

  protected render() {
    if (!this._config) return nothing;
    const lang = this.hass?.locale?.language;
    const stateObj = this._stateObj;

    if (!stateObj) {
      return html`
        <ha-card>
          <div class="warning">
            ${localize("scene_card.entity_not_found", lang)} <code>${this._config.entity}</code>
          </div>
        </ha-card>
      `;
    }

    const current = stateObj.state;
    const options: string[] = stateObj.attributes.options || [NONE_OPTION];
    const duration: number | undefined = stateObj.attributes.duration_remaining_seconds;
    const activeSceneName = current !== NONE_OPTION ? current : null;

    return html`
      <ha-card>
        <div class="header">
          <div class="title">${stateObj.attributes.friendly_name || localize("scene_card.title", lang)}</div>
        </div>
        ${
          activeSceneName
            ? html`
                <div class="status active">
                  <ha-icon icon=${iconFor(activeSceneName)}></ha-icon>
                  <div class="status-text">
                    <div class="status-main">${activeSceneName} ${localize("scene_card.active_suffix", lang)}</div>
                    ${
                      duration != null && duration > 0
                        ? html`<div class="status-sub">
                            ${formatDuration(duration)} ${localize("scene_card.remaining_suffix", lang)}
                          </div>`
                        : nothing
                    }
                  </div>
                </div>
              `
            : html`<div class="status">${localize("scene_card.running_normal_schedule", lang)}</div>`
        }
        ${this._activationError ? html`<div class="activation-error">${this._activationError}</div>` : nothing}
        <div class="tiles">${options.map((name) => this._renderTile(name, name === current))}</div>
      </ha-card>
    `;
  }

  static styles = css`
    ha-card {
      padding: 16px;
    }
    .header {
      margin-bottom: 12px;
    }
    .title {
      font-size: 1.2em;
      font-weight: 500;
      color: var(--primary-text-color);
    }
    .status {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 10px 12px;
      border-radius: 10px;
      margin-bottom: 14px;
      font-size: 0.9em;
      color: var(--secondary-text-color);
    }
    .status.active {
      background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.08);
      color: var(--primary-text-color);
    }
    .status.active ha-icon {
      color: var(--primary-color);
    }
    .status-main {
      font-weight: 500;
    }
    .status-sub {
      font-size: 0.85em;
      color: var(--secondary-text-color);
    }
    .warning {
      padding: 12px;
      color: var(--error-color, #db4437);
    }
    .tiles {
      display: flex;
      flex-wrap: wrap;
      justify-content: center;
      gap: 10px;
    }
    .tile {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 6px;
      width: 84px;
      height: 84px;
      border-radius: 12px;
      border: 1px solid var(--divider-color);
      background: var(--card-background-color);
      color: var(--primary-text-color);
      cursor: pointer;
      font-family: inherit;
      font-size: 0.75em;
      padding: 0 6px;
      position: relative;
      box-sizing: border-box;
    }
    .tile:hover {
      border-color: var(--primary-color);
    }
    .tile.active {
      border-color: var(--primary-color);
      border-width: 1.5px;
      background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.08);
      color: var(--primary-color);
      font-weight: 500;
    }
    .tile ha-icon {
      --mdc-icon-size: 22px;
    }
    .tile:disabled {
      opacity: 0.5;
      cursor: default;
    }
    .tile ha-icon.spin {
      animation: mobius-scene-spin 1s linear infinite;
    }
    @keyframes mobius-scene-spin {
      from {
        transform: rotate(0deg);
      }
      to {
        transform: rotate(360deg);
      }
    }
    .activation-error {
      margin: -4px 0 10px;
      padding: 8px 10px;
      border-radius: 8px;
      background: rgba(219, 68, 55, 0.1);
      color: var(--error-color, #db4437);
      font-size: 0.85em;
    }
    .tile .check {
      position: absolute;
      top: 4px;
      right: 4px;
      --mdc-icon-size: 16px;
      color: var(--primary-color);
      background: var(--card-background-color);
      border-radius: 50%;
    }
    .tile span {
      text-align: center;
      line-height: 1.15;
      overflow: hidden;
      text-overflow: ellipsis;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
    }
  `;
}

declare global {
  interface HTMLElementTagNameMap {
    "mobius-scene-card": MobiusSceneCard;
  }
  interface Window {
    customCards?: Array<Record<string, unknown>>;
  }
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "mobius-scene-card",
  name: "Mobius Scene",
  description: "Activate a Mobius tank's scenes, or resume its normal schedule.",
  preview: false,
});
