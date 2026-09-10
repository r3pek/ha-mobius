/**
 * Executes the ACTUAL built dist/mobius-schedule-card.js in a real
 * jsdom DOM environment -- not a mock of the card's own logic, the
 * real bundle a person's browser would load. Run with `npm test`.
 */

import { test, before } from "node:test";
import assert from "node:assert/strict";
import { JSDOM } from "jsdom";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const DIST_FILE = join(HERE, "..", "..", "custom_components", "mobius", "frontend", "dist", "mobius-schedule-card.js");

let ctor;

before(async () => {
  const dom = new JSDOM("<!DOCTYPE html><html><body></body></html>", {
    url: "http://localhost/",
    runScripts: "dangerously",
    pretendToBeVisual: true,
  });
  for (const key of Object.getOwnPropertyNames(dom.window)) {
    if (!(key in globalThis)) {
      try {
        globalThis[key] = dom.window[key];
      } catch {
        /* non-configurable global, skip */
      }
    }
  }
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;

  await import(DIST_FILE);
  ctor = customElements.get("mobius-schedule-card");
});

const LIGHT_DEVICE_ID = "light-device-1";
const PUMP_DEVICE_ID = "pump-device-1";
const OTHER_LIGHT_DEVICE_ID = "light-device-2";
const TANK_DEVICE_ID = "tank-device-1";

const LIGHT_GROUP = {
  kind: "light",
  group_mask: 12345,
  channels: ["RoyalBlue", "Violet"],
  active_scene: null,
  scene_entity_id: "select.reef_tank_scene_selection",
  schedule_intensity: 0.75,
  members: [
    { device_id: LIGHT_DEVICE_ID, serial: "SN1", name: "Left Radion" },
    { device_id: OTHER_LIGHT_DEVICE_ID, serial: "SN2", name: "Right Radion" },
  ],
};

const PUMP_GROUP = {
  kind: "pump",
  group_mask: null,
  modes: ["ConstantSpeed", "Lagoon"],
  active_scene: null,
  scene_entity_id: "select.reef_tank_scene_selection",
  members: [{ device_id: PUMP_DEVICE_ID, serial: "SN3", name: "Return Pump" }],
};

function makeHass({ devices, wsResponse, wsError } = {}) {
  return {
    devices: devices ?? { [LIGHT_DEVICE_ID]: { id: LIGHT_DEVICE_ID, via_device_id: TANK_DEVICE_ID } },
    states: {},
    callWS: async (msg) => {
      if (wsError) throw wsError;
      if (msg.type === "mobius/resolve_schedule_groups") {
        return wsResponse ?? { groups: [LIGHT_GROUP, PUMP_GROUP] };
      }
      throw new Error(`unexpected callWS message type: ${msg.type}`);
    },
  };
}

function makeCard(deviceId) {
  const el = document.createElement("mobius-schedule-card");
  el.setConfig({ device_id: deviceId });
  document.body.appendChild(el);
  return el;
}

test("registers as a custom element", () => {
  assert.ok(ctor);
});

test("registers itself in window.customCards", () => {
  assert.ok(window.customCards.some((c) => c.type === "mobius-schedule-card"));
});

test("setConfig throws without a device_id", () => {
  const el = document.createElement("mobius-schedule-card");
  assert.throws(() => el.setConfig({}), /device_id/);
});

test("setConfig accepts a valid device_id", () => {
  const el = document.createElement("mobius-schedule-card");
  assert.doesNotThrow(() => el.setConfig({ device_id: LIGHT_DEVICE_ID }));
});

test("getConfigForm restricts the device selector to the mobius integration", () => {
  const form = ctor.getConfigForm();
  const field = form.schema.find((f) => f.name === "device_id");
  assert.ok(field);
  assert.equal(field.required, true);
  assert.equal(field.selector.device.integration, "mobius");
});

test("resolves the correct light group for the configured device_id", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeHass();
  await el.updateComplete;
  // willUpdate kicks off an async resolution; wait for it to settle.
  await new Promise((r) => setTimeout(r, 0));
  await el.updateComplete;

  const text = el.shadowRoot.textContent;
  assert.ok(text.includes("Light Schedule"));
  assert.ok(text.includes("Left Radion"));
  assert.ok(text.includes("Right Radion"));
});

test("resolves the correct pump group for a different configured device_id", async () => {
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makeHass({
    devices: { [PUMP_DEVICE_ID]: { id: PUMP_DEVICE_ID, via_device_id: TANK_DEVICE_ID } },
  });
  await el.updateComplete;
  await new Promise((r) => setTimeout(r, 0));
  await el.updateComplete;

  const text = el.shadowRoot.textContent;
  assert.ok(text.includes("Pump Schedule"));
  assert.ok(text.includes("Return Pump"));
});

test("shows a clear error when the device itself can't be found", async () => {
  const el = makeCard("nonexistent-device");
  el.hass = makeHass({ devices: {} });
  await el.updateComplete;
  await new Promise((r) => setTimeout(r, 0));
  await el.updateComplete;

  assert.ok(el.shadowRoot.textContent.includes("could not be found"));
});

test("shows a clear error when the device has no via_device_id (not linked to a tank)", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeHass({
    devices: { [LIGHT_DEVICE_ID]: { id: LIGHT_DEVICE_ID, via_device_id: null } },
  });
  await el.updateComplete;
  await new Promise((r) => setTimeout(r, 0));
  await el.updateComplete;

  assert.ok(el.shadowRoot.textContent.includes("Mobius tank"));
});

test("shows a clear error when no returned group actually contains the device", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeHass({ wsResponse: { groups: [PUMP_GROUP] } });
  await el.updateComplete;
  await new Promise((r) => setTimeout(r, 0));
  await el.updateComplete;

  assert.ok(el.shadowRoot.textContent.includes("own schedule group"));
});

test("shows a clear error when the websocket call itself fails", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeHass({ wsError: new Error("connection lost") });
  await el.updateComplete;
  await new Promise((r) => setTimeout(r, 0));
  await el.updateComplete;

  assert.ok(el.shadowRoot.textContent.includes("connection lost"));
});

test("does not re-resolve on every hass update -- only when device_id changes", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  let callCount = 0;
  const hass = makeHass();
  const originalCallWS = hass.callWS;
  hass.callWS = async (msg) => {
    callCount++;
    return originalCallWS(msg);
  };
  el.hass = hass;
  await el.updateComplete;
  await new Promise((r) => setTimeout(r, 0));
  await el.updateComplete;
  assert.equal(callCount, 1);

  // A second, unrelated hass update (e.g. some other entity changed
  // state elsewhere in the system) must not trigger a second
  // resolution -- this is structural metadata, not a live-data feed.
  el.hass = { ...hass };
  await el.updateComplete;
  await new Promise((r) => setTimeout(r, 0));
  await el.updateComplete;
  assert.equal(callCount, 1);
});

test("re-resolves when the configured device_id itself changes", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeHass();
  await el.updateComplete;
  await new Promise((r) => setTimeout(r, 0));
  await el.updateComplete;
  assert.ok(el.shadowRoot.textContent.includes("Light Schedule"));

  el.setConfig({ device_id: PUMP_DEVICE_ID });
  el.hass = makeHass({
    devices: { [PUMP_DEVICE_ID]: { id: PUMP_DEVICE_ID, via_device_id: TANK_DEVICE_ID } },
  });
  await el.updateComplete;
  await new Promise((r) => setTimeout(r, 0));
  await el.updateComplete;
  assert.ok(el.shadowRoot.textContent.includes("Pump Schedule"));
});
