/**
 * Executes the ACTUAL built dist/mobius-schedule-card.js in a real
 * jsdom DOM environment -- not a mock of the card's own logic, the
 * real bundle a person's browser would load. Run with `npm test`.
 */

import { test, before, afterEach } from "node:test";
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
  // Node 18+ ships its own native CustomEvent/Event globals, which
  // the loop above skips (they're already "in globalThis") -- but
  // dispatchEvent on a JSDOM node only recognizes JSDOM's own Event
  // class hierarchy, not Node's native one, so code under test
  // constructing `new CustomEvent(...)` would otherwise throw
  // "parameter 1 is not of type 'Event'" the moment it tries to
  // dispatch. Force these two specifically to JSDOM's own versions.
  globalThis.CustomEvent = dom.window.CustomEvent;
  globalThis.Event = dom.window.Event;
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;

  // JSDOM itself doesn't implement these (a real browser always
  // would) -- mocked minimally so the .mob download flow can run at
  // all in tests, without needing real blob-URL semantics.
  window.URL.createObjectURL = () => "blob:mock-url";
  window.URL.revokeObjectURL = () => {};

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
    {
      device_id: LIGHT_DEVICE_ID,
      serial: "SN1",
      name: "Left Radion",
      channel_entity_ids: { RoyalBlue: "sensor.left_royalblue", Violet: "sensor.left_violet" },
      schedule_intensity_entity_id: "sensor.left_schedule_intensity",
    },
    {
      device_id: OTHER_LIGHT_DEVICE_ID,
      serial: "SN2",
      name: "Right Radion",
      channel_entity_ids: { RoyalBlue: "sensor.right_royalblue", Violet: "sensor.right_violet" },
      schedule_intensity_entity_id: "sensor.right_schedule_intensity",
    },
  ],
};

const PUMP_GROUP = {
  kind: "pump",
  group_mask: null,
  modes: ["ConstantSpeed", "Lagoon", "Sync", "EcoSmartBack"],
  mode_params: {
    ConstantSpeed: ["MaxSpeed"],
    Lagoon: ["MaxSpeed"],
    Sync: ["MaxSpeed", "PhaseShift", "ParentSerial"],
    EcoSmartBack: ["MaxSpeed", "PhaseShift", "ParentSerial"],
  },
  active_scene: null,
  scene_entity_id: "select.reef_tank_scene_selection",
  members: [
    {
      device_id: PUMP_DEVICE_ID,
      serial: "SN3",
      name: "Return Pump",
      flow_entity_id: "sensor.pump_flow",
      speed_entity_id: "sensor.pump_speed",
      mode_entity_id: "sensor.pump_mode",
    },
  ],
};

function makeHass({
  devices,
  wsResponse,
  wsError,
  historyResponse,
  states,
  scheduleResponse,
  scheduleError,
  exportMobResponse,
  exportMobError,
  parseMobResponse,
  parseMobError,
} = {}) {
  return {
    devices: devices ?? { [LIGHT_DEVICE_ID]: { id: LIGHT_DEVICE_ID, via_device_id: TANK_DEVICE_ID } },
    states: states ?? {},
    callWS: async (msg) => {
      if (wsError) throw wsError;
      if (msg.type === "mobius/resolve_schedule_groups") {
        return wsResponse ?? { groups: [LIGHT_GROUP, PUMP_GROUP] };
      }
      if (msg.type === "history/history_during_period") {
        return historyResponse ?? {};
      }
      if (msg.type === "mobius/read_schedule_group") {
        if (scheduleError) throw scheduleError;
        return scheduleResponse ?? { points: [] };
      }
      if (msg.type === "mobius/export_schedule_group_mob") {
        if (exportMobError) throw exportMobError;
        return exportMobResponse ?? { mob: { schedules: [] } };
      }
      if (msg.type === "mobius/parse_schedule_mob") {
        if (parseMobError) throw parseMobError;
        return parseMobResponse ?? { points: [] };
      }
      throw new Error(`unexpected callWS message type: ${msg.type}`);
    },
  };
}

// Every card this file creates gets removed after its own test --
// this isn't just tidiness: mobius-schedule-card sets up a real
// setInterval (for periodic history refresh) that only gets cleared
// in disconnectedCallback. Leaving elements attached to document.body
// for the whole file would leave every one of those intervals running
// for its full 5-minute period, which (being real timers, not fake
// ones, in every test that doesn't explicitly enable mock.timers)
// keeps the Node process itself alive and hangs the entire test run.
const _createdElements = [];
afterEach(() => {
  for (const el of _createdElements) el.remove();
  _createdElements.length = 0;
});

function makeCard(deviceId) {
  const el = document.createElement("mobius-schedule-card");
  el.setConfig({ device_id: deviceId });
  document.body.appendChild(el);
  _createdElements.push(el);
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
  let resolveCallCount = 0;
  const hass = makeHass();
  const originalCallWS = hass.callWS;
  hass.callWS = async (msg) => {
    if (msg.type === "mobius/resolve_schedule_groups") resolveCallCount++;
    return originalCallWS(msg);
  };
  el.hass = hass;
  await el.updateComplete;
  await new Promise((r) => setTimeout(r, 0));
  await el.updateComplete;
  assert.equal(resolveCallCount, 1);

  // A second, unrelated hass update (e.g. some other entity changed
  // state elsewhere in the system) must not trigger a second
  // resolution -- this is structural metadata, not a live-data feed.
  el.hass = { ...hass };
  await el.updateComplete;
  await new Promise((r) => setTimeout(r, 0));
  await el.updateComplete;
  assert.equal(resolveCallCount, 1);
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

// --------------------------------------------------------------------------
// Pump glance view
// --------------------------------------------------------------------------

function makePumpHass(
  states,
  {
    scheduleResponse,
    scheduleError,
    wsResponse,
    exportMobResponse,
    exportMobError,
    parseMobResponse,
    parseMobError,
  } = {},
) {
  return makeHass({
    devices: { [PUMP_DEVICE_ID]: { id: PUMP_DEVICE_ID, via_device_id: TANK_DEVICE_ID } },
    states,
    scheduleResponse,
    scheduleError,
    wsResponse,
    exportMobResponse,
    exportMobError,
    parseMobResponse,
    parseMobError,
  });
}

function makeLightHass(states, historyResponse, { scheduleResponse, scheduleError } = {}) {
  return makeHass({
    devices: { [LIGHT_DEVICE_ID]: { id: LIGHT_DEVICE_ID, via_device_id: TANK_DEVICE_ID } },
    states,
    historyResponse,
    scheduleResponse,
    scheduleError,
  });
}

async function settled(el) {
  await el.updateComplete;
  await new Promise((r) => setTimeout(r, 0));
  await el.updateComplete;
}

test("pump glance shows flow using whatever unit is configured on the sensor (L/h)", async () => {
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass({
    "sensor.pump_flow": { state: "412", attributes: { unit_of_measurement: "L/h" } },
  });
  await settled(el);

  const text = el.shadowRoot.textContent;
  assert.ok(text.includes("412"));
  assert.ok(text.includes("L/h"));
});

test("pump glance rounds a decimal flow reading to a whole number", async () => {
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass({
    "sensor.pump_flow": { state: "412.73", attributes: { unit_of_measurement: "GPH" } },
  });
  await settled(el);

  assert.equal(el.shadowRoot.querySelector(".reading-value").textContent, "413");
});

test("pump glance rounds a decimal speed reading (fallback path) to a whole number", async () => {
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass({
    "sensor.pump_speed": { state: "42.4", attributes: {} },
  });
  await settled(el);

  assert.equal(el.shadowRoot.querySelector(".reading-value").textContent, "42%");
});

test("pump glance respects a different configured unit (GPH) -- never assumes L/h", async () => {
  // The device itself always reports GPH; Home Assistant's own
  // per-entity unit override converts both state and
  // unit_of_measurement server-side for volume_flow_rate sensors, so
  // a person who's switched their own sensor to a different unit
  // (or left it at the device's native GPH) must see THAT unit, not
  // a hardcoded assumption of L/h.
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass({
    "sensor.pump_flow": { state: "108", attributes: { unit_of_measurement: "gal/h" } },
  });
  await settled(el);

  const text = el.shadowRoot.textContent;
  assert.ok(text.includes("108"));
  assert.ok(text.includes("gal/h"));
  assert.ok(!text.includes("L/h"));
});

test("pump glance falls back to speed % when the flow sensor is unavailable", async () => {
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass({
    "sensor.pump_flow": { state: "unavailable", attributes: {} },
    "sensor.pump_speed": { state: "58", attributes: {} },
  });
  await settled(el);

  const text = el.shadowRoot.textContent;
  assert.ok(text.includes("58%"));
  assert.ok(text.includes("not reliable"));
});

test("pump glance shows a clear message when neither flow nor speed is available", async () => {
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass({});
  await settled(el);

  assert.ok(el.shadowRoot.textContent.includes("No flow or speed data"));
});

test("pump glance shows the current mode when available", async () => {
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass({
    "sensor.pump_flow": { state: "300", attributes: {} },
    "sensor.pump_mode": { state: "TidalSwell", attributes: {} },
  });
  await settled(el);

  const text = el.shadowRoot.textContent;
  assert.ok(text.includes("Currently running"));
  assert.ok(text.includes("TidalSwell"));
});

test("pump glance has an edit button", async () => {
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass({ "sensor.pump_flow": { state: "300", attributes: {} } });
  await settled(el);

  assert.ok(el.shadowRoot.querySelector(".edit-button"));
});

test("pump glance updates the shown flow value when the entity's own state changes", async () => {
  // Unlike the chart (a point-in-time history snapshot that needs its
  // own periodic re-fetch to avoid going stale), the current flow
  // reading is read live from hass.states on every render with no
  // caching at all -- it needs no equivalent refresh mechanism, only
  // a genuine hass reassignment, exactly like Home Assistant's own
  // frontend performs on every real entity state change.
  const el = makeCard(PUMP_DEVICE_ID);
  const hass = makePumpHass({ "sensor.pump_flow": { state: "300", attributes: {} } });
  el.hass = hass;
  await settled(el);
  // .reading-value specifically, not the whole shadow root's own
  // textContent -- that also includes the <style> tag's own CSS text,
  // which happens to contain the literal substring "300" from an
  // unrelated rule (font-weight: 300), an easy false negative/positive
  // trap for a plain substring check here.
  assert.equal(el.shadowRoot.querySelector(".reading-value").textContent.trim(), "300");

  el.hass = { ...hass, states: { ...hass.states, "sensor.pump_flow": { state: "450", attributes: {} } } };
  await el.updateComplete;

  assert.equal(el.shadowRoot.querySelector(".reading-value").textContent.trim(), "450");
});

test("scene banner shows the active scene and its remaining time", async () => {
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass({
    "sensor.pump_flow": { state: "300", attributes: {} },
    "select.reef_tank_scene_selection": {
      state: "Feeding",
      attributes: { duration_remaining_seconds: 125 },
    },
  });
  await settled(el);

  const text = el.shadowRoot.textContent;
  assert.ok(text.includes("Feeding"));
  assert.ok(text.includes("2:05"));
  assert.ok(text.includes("remaining"));
});

test("scene banner is hidden when no scene is active (state is None)", async () => {
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass({
    "sensor.pump_flow": { state: "300", attributes: {} },
    "select.reef_tank_scene_selection": { state: "None", attributes: {} },
  });
  await settled(el);

  assert.equal(el.shadowRoot.querySelector(".scene-banner"), null);
});

test("scene banner is hidden when the scene entity itself is missing", async () => {
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass({ "sensor.pump_flow": { state: "300", attributes: {} } });
  await settled(el);

  assert.equal(el.shadowRoot.querySelector(".scene-banner"), null);
});

// --------------------------------------------------------------------------
// Light glance view -- sensor fallback and overall intensity slider
// --------------------------------------------------------------------------

test("light glance shows no unavailable-note when both members are online", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeLightHass({
    "sensor.left_royalblue": { state: "60", attributes: {} },
    "sensor.right_royalblue": { state: "60", attributes: {} },
  });
  await settled(el);

  assert.equal(el.shadowRoot.querySelector(".unavailable-note"), null);
});

test("light glance names exactly one unavailable member (singular wording)", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeLightHass({
    "sensor.left_royalblue": { state: "unavailable", attributes: {} },
    "sensor.right_royalblue": { state: "60", attributes: {} },
  });
  await settled(el);

  const text = el.shadowRoot.textContent;
  assert.ok(text.includes("Left Radion"));
  assert.ok(text.includes("is unavailable"));
  assert.ok(!text.includes("Right Radion is unavailable"));
});

test("light glance names both unavailable members with 'and' (plural wording)", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeLightHass({
    "sensor.left_royalblue": { state: "unavailable", attributes: {} },
    "sensor.right_royalblue": { state: "unavailable", attributes: {} },
  });
  await settled(el);

  const text = el.shadowRoot.textContent;
  assert.ok(text.includes("Left Radion and Right Radion"));
  assert.ok(text.includes("are unavailable"));
});

test("light glance shows a clear message when every member is unavailable", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeLightHass({
    "sensor.left_royalblue": { state: "unavailable", attributes: {} },
    "sensor.right_royalblue": { state: "unavailable", attributes: {} },
  });
  await settled(el);

  assert.ok(el.shadowRoot.textContent.includes("No channel data available"));
});

test("light glance shows the live overall intensity percentage", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeLightHass({
    "sensor.left_royalblue": { state: "60", attributes: {} },
    "sensor.left_schedule_intensity": { state: "59", attributes: {} },
  });
  await settled(el);

  const text = el.shadowRoot.textContent;
  assert.ok(text.includes("Overall intensity"));
  assert.ok(text.includes("59%"));
});

test("light glance has an edit button", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeLightHass({ "sensor.left_royalblue": { state: "60", attributes: {} } });
  await settled(el);

  assert.ok(el.shadowRoot.querySelector(".edit-button"));
});

test("dragging the intensity slider updates the displayed value immediately, before the debounced write", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  const hass = makeLightHass({
    "sensor.left_royalblue": { state: "60", attributes: {} },
    "sensor.left_schedule_intensity": { state: "50", attributes: {} },
  });
  const calls = [];
  hass.callService = async (domain, service, data) => {
    calls.push({ domain, service, data });
  };
  el.hass = hass;
  await settled(el);

  const slider = el.shadowRoot.querySelector('input[type="range"]');
  slider.value = "75";
  slider.dispatchEvent(new window.Event("input"));
  await el.updateComplete;

  assert.ok(el.shadowRoot.textContent.includes("75%"));
  assert.equal(calls.length, 0, "service must not be called yet -- still within the debounce window");
});

test("the debounced write eventually calls mobius.set_schedule_intensity with the right device_id and value", async (t) => {
  const el = makeCard(LIGHT_DEVICE_ID);
  const hass = makeLightHass({
    "sensor.left_royalblue": { state: "60", attributes: {} },
    "sensor.left_schedule_intensity": { state: "50", attributes: {} },
  });
  const calls = [];
  hass.callService = async (domain, service, data) => {
    calls.push({ domain, service, data });
  };
  el.hass = hass;
  await settled(el);

  // Only enabled AFTER the card's own initial resolution has already
  // settled using real timers -- settled() itself relies on a real
  // setTimeout(0) internally, which would otherwise also get mocked
  // and never fire on its own.
  t.mock.timers.enable({ apis: ["setTimeout"] });

  const slider = el.shadowRoot.querySelector('input[type="range"]');
  slider.value = "75";
  slider.dispatchEvent(new window.Event("input"));

  t.mock.timers.tick(1200);
  // Microtask-based waiting here, not settled()'s own setTimeout(0) --
  // that would itself be mocked at this point in the test, and never
  // fire without yet another manual tick().
  await Promise.resolve();
  await Promise.resolve();
  await el.updateComplete;

  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], {
    domain: "mobius",
    service: "set_schedule_intensity",
    data: { device_id: LIGHT_DEVICE_ID, intensity: 75 },
  });
});

// --------------------------------------------------------------------------
// Channel history chart
// --------------------------------------------------------------------------

function historyEntry(value, secondsSinceMidnight) {
  const midnight = new Date();
  midnight.setHours(0, 0, 0, 0);
  return { s: String(value), lu: midnight.getTime() / 1000 + secondsSinceMidnight };
}

test("chart renders a polyline for the source member's own channel history", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeLightHass(
    { "sensor.left_royalblue": { state: "60", attributes: {} } },
    {
      "sensor.left_royalblue": [historyEntry(0, 0), historyEntry(80, 3600), historyEntry(60, 7200)],
    },
  );
  await settled(el);

  const polylines = el.shadowRoot.querySelectorAll(".chart polyline");
  assert.equal(polylines.length, 1);
  assert.ok(polylines[0].getAttribute("points").includes(","));
});

test("chart draws one polyline per channel with real history data", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeLightHass(
    { "sensor.left_royalblue": { state: "60", attributes: {} } },
    {
      "sensor.left_royalblue": [historyEntry(50, 0)],
      "sensor.left_violet": [historyEntry(30, 0)],
    },
  );
  await settled(el);

  assert.equal(el.shadowRoot.querySelectorAll(".chart polyline").length, 2);
});

test("chart shows a clear message when the group has channels but no history yet", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeLightHass({ "sensor.left_royalblue": { state: "60", attributes: {} } }, {});
  await settled(el);

  assert.ok(el.shadowRoot.textContent.includes("No history recorded yet today"));
});

test("chart uses the fallback member's own history when the primary member is unavailable", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeLightHass(
    {
      "sensor.left_royalblue": { state: "unavailable", attributes: {} },
      "sensor.right_royalblue": { state: "60", attributes: {} },
    },
    {
      "sensor.left_royalblue": [historyEntry(99, 0)],
      "sensor.right_royalblue": [historyEntry(40, 0)],
    },
  );
  await settled(el);

  // Only the fallback (Right Radion) member's own channels should be
  // charted -- not the unavailable primary member's stale data.
  const polylines = el.shadowRoot.querySelectorAll(".chart polyline");
  assert.equal(polylines.length, 1);
});

test("chart shows hour gridline labels", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeLightHass(
    { "sensor.left_royalblue": { state: "60", attributes: {} } },
    { "sensor.left_royalblue": [historyEntry(50, 0)] },
  );
  await settled(el);

  assert.equal(el.shadowRoot.querySelectorAll(".chart-label").length, 4);
});

// --------------------------------------------------------------------------
// Chart memoization and periodic history refresh
// --------------------------------------------------------------------------

test("chart computation is memoized -- an unrelated hass update does not recompute it", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  const hass = makeLightHass(
    { "sensor.left_royalblue": { state: "60", attributes: {} } },
    { "sensor.left_royalblue": [historyEntry(50, 0)] },
  );
  el.hass = hass;
  await settled(el);

  let computeCount = 0;
  const original = ctor.prototype._computeChannelChart;
  ctor.prototype._computeChannelChart = function (...args) {
    computeCount++;
    return original.apply(this, args);
  };
  try {
    // Simulates exactly what real dashboard traffic looks like: hass
    // reassigned as a brand new object (as HA's own frontend does on
    // every state change anywhere in the system), with the actual
    // history data completely unchanged.
    el.hass = { ...hass };
    await el.updateComplete;
    el.hass = { ...hass };
    await el.updateComplete;
    el.hass = { ...hass };
    await el.updateComplete;

    assert.equal(computeCount, 0, "unrelated hass reassignment must not recompute the chart");
  } finally {
    ctor.prototype._computeChannelChart = original;
  }
});

test("chart recomputes once real history data actually changes", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  const hass = makeLightHass(
    { "sensor.left_royalblue": { state: "60", attributes: {} } },
    { "sensor.left_royalblue": [historyEntry(50, 0)] },
  );
  el.hass = hass;
  await settled(el);

  let computeCount = 0;
  const original = ctor.prototype._computeChannelChart;
  ctor.prototype._computeChannelChart = function (...args) {
    computeCount++;
    return original.apply(this, args);
  };
  try {
    // A genuinely new history payload (as a periodic refresh would
    // produce) is a different object reference, and must trigger a
    // real recomputation.
    el._channelHistoryByEntity = { "sensor.left_royalblue": [historyEntry(70, 0)] };
    await el.updateComplete;

    assert.equal(computeCount, 1);
  } finally {
    ctor.prototype._computeChannelChart = original;
  }
});

test("history is refetched periodically, not just once at resolution", async (t) => {
  // Enabled BEFORE hass is assigned -- the setInterval call itself
  // happens synchronously inside group resolution, so enabling the
  // mock any later would leave that real, un-mocked interval outside
  // the mock's control entirely (tick() only affects intervals
  // created after the mock itself is enabled). settled()'s own
  // internal setTimeout(0) is unaffected, since only "setInterval" is
  // in the mocked api list here.
  t.mock.timers.enable({ apis: ["setInterval"] });

  const el = makeCard(LIGHT_DEVICE_ID);
  let fetchCount = 0;
  const hass = makeLightHass({ "sensor.left_royalblue": { state: "60", attributes: {} } }, {});
  const originalCallWS = hass.callWS;
  hass.callWS = async (msg) => {
    if (msg.type === "history/history_during_period") fetchCount++;
    return originalCallWS(msg);
  };
  el.hass = hass;
  await settled(el);
  assert.equal(fetchCount, 1, "the initial resolution should fetch history exactly once");

  t.mock.timers.tick(5 * 60 * 1000);
  await Promise.resolve();
  await Promise.resolve();

  assert.equal(fetchCount, 2, "a periodic refresh should have fetched history again");
});

test("the periodic refresh timer is cleared on disconnect -- no fetches after the card leaves the DOM", async (t) => {
  t.mock.timers.enable({ apis: ["setInterval"] });

  const el = makeCard(LIGHT_DEVICE_ID);
  let fetchCount = 0;
  const hass = makeLightHass({ "sensor.left_royalblue": { state: "60", attributes: {} } }, {});
  const originalCallWS = hass.callWS;
  hass.callWS = async (msg) => {
    if (msg.type === "history/history_during_period") fetchCount++;
    return originalCallWS(msg);
  };
  el.hass = hass;
  await settled(el);
  assert.equal(fetchCount, 1);

  el.remove();
  _createdElements.splice(_createdElements.indexOf(el), 1);

  t.mock.timers.tick(30 * 60 * 1000); // well past several refresh cycles
  await Promise.resolve();
  await Promise.resolve();

  assert.equal(fetchCount, 1, "no further fetches should occur once the card has disconnected");
});

// --------------------------------------------------------------------------
// Channel colors -- moonlight channels and the DeepRed-style naming fix
// --------------------------------------------------------------------------

const MOONLIGHT_GROUP = {
  kind: "light",
  group_mask: 1,
  channels: ["MoonlightBlue", "DeepRed"],
  active_scene: null,
  scene_entity_id: "select.reef_tank_scene_selection",
  schedule_intensity: 0.5,
  members: [
    {
      device_id: LIGHT_DEVICE_ID,
      serial: "SN1",
      name: "Left Radion",
      channel_entity_ids: { MoonlightBlue: "sensor.left_moonlightblue", DeepRed: "sensor.left_deepred" },
      schedule_intensity_entity_id: "sensor.left_schedule_intensity",
    },
  ],
};

function makeMoonlightHass(states, historyResponse) {
  return makeHass({
    devices: { [LIGHT_DEVICE_ID]: { id: LIGHT_DEVICE_ID, via_device_id: TANK_DEVICE_ID } },
    wsResponse: { groups: [MOONLIGHT_GROUP] },
    states,
    historyResponse,
  });
}

test("a Moonlight-family channel (real PascalCase name, e.g. MoonlightBlue) gets its own dedicated color, not the generic fallback palette", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeMoonlightHass(
    { "sensor.left_moonlightblue": { state: "10", attributes: {} } },
    { "sensor.left_moonlightblue": [historyEntry(10, 0)], "sensor.left_deepred": [historyEntry(5, 0)] },
  );
  await settled(el);

  const polylines = [...el.shadowRoot.querySelectorAll(".chart polyline")];
  const moonlightLine = polylines.find((p) => p.getAttribute("stroke") === "#5c6bc0");
  assert.ok(moonlightLine, "MoonlightBlue should render with its own dedicated color (#5c6bc0)");
});

test("DeepRed (the real, space-free channel name python-mobius actually reports) gets its own dedicated color", async () => {
  // Regression test for a real bug: the color map's own key used to be
  // "deep red" (with a space), but VisualID.DeepRed.name is "DeepRed"
  // (PascalCase, no space) -- meaning the old entry could never have
  // matched a single real channel, ever, and every DeepRed channel
  // silently fell through to the generic hash-based fallback instead.
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeMoonlightHass(
    { "sensor.left_moonlightblue": { state: "10", attributes: {} } },
    { "sensor.left_moonlightblue": [historyEntry(10, 0)], "sensor.left_deepred": [historyEntry(5, 0)] },
  );
  await settled(el);

  const polylines = [...el.shadowRoot.querySelectorAll(".chart polyline")];
  const deepRedLine = polylines.find((p) => p.getAttribute("stroke") === "#b71c1c");
  assert.ok(deepRedLine, "DeepRed should render with its own dedicated color (#b71c1c), not a fallback color");
});

test("a genuinely unrecognized channel name still falls through to the fallback palette (not left uncolored)", async () => {
  const CUSTOM_GROUP = {
    ...MOONLIGHT_GROUP,
    channels: ["SomeCustomChannel"],
    members: [
      {
        ...MOONLIGHT_GROUP.members[0],
        channel_entity_ids: { SomeCustomChannel: "sensor.left_custom" },
      },
    ],
  };
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeHass({
    devices: { [LIGHT_DEVICE_ID]: { id: LIGHT_DEVICE_ID, via_device_id: TANK_DEVICE_ID } },
    wsResponse: { groups: [CUSTOM_GROUP] },
    states: { "sensor.left_custom": { state: "40", attributes: {} } },
    historyResponse: { "sensor.left_custom": [historyEntry(40, 0)] },
  });
  await settled(el);

  const polyline = el.shadowRoot.querySelector(".chart polyline");
  assert.ok(polyline);
  assert.ok(/^#[0-9a-f]{6}$/i.test(polyline.getAttribute("stroke")));
});

// --------------------------------------------------------------------------
// Edit view -- pump point list (read-only first slice)
// --------------------------------------------------------------------------

const SAMPLE_PUMP_POINTS = [
  { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { Speed: 300 } }, // Day
  { time_minutes: 480, flags: 3, mode: "Feed", params: {} }, // Night (1|2)
  { time_minutes: 360, flags: 7, mode: "TidalSwell", params: {} }, // Sunrise (1|6)
  { time_minutes: 1200, flags: 11, mode: "ConstantSpeed", params: {} }, // Sunset (1|10)
];

test("clicking Edit schedule fetches the real schedule and switches to the edit view", async () => {
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass(
    { "sensor.pump_flow": { state: "300", attributes: {} } },
    { scheduleResponse: { points: SAMPLE_PUMP_POINTS } },
  );
  await settled(el);

  el.shadowRoot.querySelector(".edit-button").click();
  await settled(el);

  assert.equal(el.shadowRoot.querySelectorAll(".point-row").length, 4);
  assert.ok(el.shadowRoot.querySelector(".back-button"));
});

test("edit view shows each point's own time, decoded period, and mode", async () => {
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass(
    { "sensor.pump_flow": { state: "300", attributes: {} } },
    { scheduleResponse: { points: SAMPLE_PUMP_POINTS } },
  );
  await settled(el);
  el.shadowRoot.querySelector(".edit-button").click();
  await settled(el);

  const rows = [...el.shadowRoot.querySelectorAll(".point-row")];
  const text = rows.map((r) => r.textContent);

  assert.ok(text[0].includes("0:00"));
  assert.ok(text[0].includes("Day"));
  assert.ok(text[0].includes("ConstantSpeed"));

  assert.ok(text[1].includes("8:00"));
  assert.ok(text[1].includes("Night"));

  assert.ok(text[2].includes("6:00"));
  assert.ok(text[2].includes("Sunrise"));

  assert.ok(text[3].includes("20:00"));
  assert.ok(text[3].includes("Sunset"));
});

test("the back button returns to the glance view without losing state", async () => {
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass(
    { "sensor.pump_flow": { state: "300", attributes: {} } },
    { scheduleResponse: { points: SAMPLE_PUMP_POINTS } },
  );
  await settled(el);
  el.shadowRoot.querySelector(".edit-button").click();
  await settled(el);
  assert.ok(el.shadowRoot.querySelector(".point-list"));

  el.shadowRoot.querySelector(".back-button").click();
  await el.updateComplete;

  assert.equal(el.shadowRoot.querySelector(".point-list"), null);
  assert.ok(el.shadowRoot.querySelector(".edit-button"));
});

test("edit view shows a clear error when the schedule fetch fails", async () => {
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass(
    { "sensor.pump_flow": { state: "300", attributes: {} } },
    { scheduleError: new Error("relay connection lost") },
  );
  await settled(el);
  el.shadowRoot.querySelector(".edit-button").click();
  await settled(el);

  assert.ok(el.shadowRoot.textContent.includes("relay connection lost"));
  assert.ok(el.shadowRoot.querySelector(".back-button"));
});

test("the header's own Load/Download buttons stay available even when the schedule fetch itself fails", async () => {
  // They live in the shell's own header now, independent of the
  // schedule fetch -- a person should still be able to load a fresh
  // .mob file even if reading the current schedule off the device
  // failed.
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass(
    { "sensor.pump_flow": { state: "300", attributes: {} } },
    { scheduleError: new Error("relay connection lost") },
  );
  await settled(el);
  el.shadowRoot.querySelector(".edit-button").click();
  await settled(el);

  assert.ok(el.shadowRoot.querySelector('.header-icon-button[title="Load .mob"]'));
  assert.ok(el.shadowRoot.querySelector('.header-icon-button[title="Download .mob"]'));
});

test("edit view can still navigate back after a failed fetch", async () => {
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass(
    { "sensor.pump_flow": { state: "300", attributes: {} } },
    { scheduleError: new Error("relay connection lost") },
  );
  await settled(el);
  el.shadowRoot.querySelector(".edit-button").click();
  await settled(el);

  el.shadowRoot.querySelector(".back-button").click();
  await el.updateComplete;

  assert.ok(el.shadowRoot.querySelector(".edit-button"));
});

// --------------------------------------------------------------------------
// Point editing
// --------------------------------------------------------------------------

async function openEditWithPoints(points) {
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass({ "sensor.pump_flow": { state: "300", attributes: {} } }, { scheduleResponse: { points } });
  await settled(el);
  el.shadowRoot.querySelector(".edit-button").click();
  await settled(el);
  return el;
}

test("clicking a point row opens an edit form pre-filled with its own values", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 90, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);

  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;

  const form = el.shadowRoot.querySelector(".point-edit-form");
  assert.ok(form);
  assert.equal(el.shadowRoot.querySelector('input[type="time"]').value, "01:30");
  assert.equal(el.shadowRoot.querySelector(".mode-label select").value, "ConstantSpeed");
  assert.equal(el.shadowRoot.querySelector(".param-label input").value, "30");
});

test("switching to Sync shows MaxSpeed and the parent-pump picker, but never a raw PhaseShift field", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;
  assert.equal(el.shadowRoot.querySelectorAll(".param-label").length, 1);

  const modeSelect = el.shadowRoot.querySelector(".mode-label select");
  modeSelect.value = "Sync";
  modeSelect.dispatchEvent(new window.Event("change"));
  await el.updateComplete;

  // PhaseShift is deliberately absent -- it's fully implied by which
  // of Sync/Anti-Sync/EcoSmart Back the mode dropdown itself shows,
  // matching the real app's own UI (which never exposes a raw phase
  // value to a person at all).
  const paramLabels = [...el.shadowRoot.querySelectorAll(".param-label")].map((l) => l.textContent);
  assert.equal(paramLabels.length, 2);
  assert.ok(paramLabels.some((t) => t.includes("MaxSpeed")));
  assert.ok(paramLabels.some((t) => t.includes("Parent pump")));
  assert.ok(!paramLabels.some((t) => t.includes("PhaseShift")));
});

test("switching mode carries over a shared param's own value", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 450 } },
  ]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;

  const modeSelect = el.shadowRoot.querySelector(".mode-label select");
  modeSelect.value = "Lagoon"; // also just [MaxSpeed]
  modeSelect.dispatchEvent(new window.Event("change"));
  await el.updateComplete;

  assert.equal(el.shadowRoot.querySelector(".param-label input").value, "45");
});

test("Sync and EcoSmartBack show the identical set of param fields", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "Sync", params: { MaxSpeed: 300, PhaseShift: 180, ParentSerial: "SN4" } },
  ]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;
  const syncLabels = [...el.shadowRoot.querySelectorAll(".param-label")].map((l) => l.textContent.trim());

  const modeSelect = el.shadowRoot.querySelector(".mode-label select");
  modeSelect.value = "EcoSmartBack";
  modeSelect.dispatchEvent(new window.Event("change"));
  await el.updateComplete;
  const ecoLabels = [...el.shadowRoot.querySelectorAll(".param-label")].map((l) => l.textContent.trim());

  assert.deepEqual(syncLabels, ecoLabels);
});

test("RampType renders as a dropdown with the three real values", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;
  const modeSelect = el.shadowRoot.querySelector(".mode-label select");

  // Transition isn't in this group's own modes list in this test, so
  // exercise the RampType rendering path directly via the working
  // point instead of relying on a mode switch that may not be offered.
  el._workingPoint = { ...el._workingPoint, mode: "Transition", params: { RampType: "Linear" } };
  el._group = { ...el._group, mode_params: { ...el._group.mode_params, Transition: ["RampType"] } };
  await el.updateComplete;

  const select = el.shadowRoot.querySelector(".param-label select");
  assert.ok(select);
  const options = [...select.querySelectorAll("option")].map((o) => o.value);
  assert.deepEqual(options, ["Sinusoidal", "Logarithmic", "Linear"]);
  assert.equal(select.value, "Linear");
  void modeSelect;
});

test("editing a param and saving updates the point list", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;

  const input = el.shadowRoot.querySelector(".param-label input");
  input.value = "500";
  input.dispatchEvent(new window.Event("change"));
  await el.updateComplete;

  el.shadowRoot.querySelector(".save-point-button").click();
  await el.updateComplete;

  assert.equal(el.shadowRoot.querySelector(".point-edit-form"), null);
  assert.ok(el.shadowRoot.querySelector(".point-row").textContent.includes("ConstantSpeed"));
});

test("cancel discards changes and leaves the original point untouched", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;

  const modeSelect = el.shadowRoot.querySelector(".mode-label select");
  modeSelect.value = "Lagoon";
  modeSelect.dispatchEvent(new window.Event("change"));
  await el.updateComplete;

  el.shadowRoot.querySelector(".cancel-button").click();
  await el.updateComplete;

  assert.equal(el.shadowRoot.querySelector(".point-edit-form"), null);
  assert.ok(el.shadowRoot.querySelector(".point-row").textContent.includes("ConstantSpeed"));
  assert.ok(!el.shadowRoot.querySelector(".point-row").textContent.includes("Lagoon"));
});

test("period dropdown edits the point's own flags correctly", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;

  const periodSelect = el.shadowRoot.querySelectorAll(".edit-row select")[0];
  periodSelect.value = "sunset";
  periodSelect.dispatchEvent(new window.Event("change"));
  await el.updateComplete;

  el.shadowRoot.querySelector(".save-point-button").click();
  await el.updateComplete;

  assert.equal(el._schedulePoints[0].flags, 11);
  assert.ok(el.shadowRoot.querySelector(".point-row").textContent.includes("Sunset"));
});

// --------------------------------------------------------------------------
// Parent-pump picker (Sync/EcoSmartBack's own ParentSerial field)
// --------------------------------------------------------------------------

const OTHER_PUMP_GROUP = {
  kind: "pump",
  group_mask: null,
  modes: ["ConstantSpeed"],
  mode_params: { ConstantSpeed: ["MaxSpeed"] },
  active_scene: null,
  scene_entity_id: "select.reef_tank_scene_selection",
  members: [{ device_id: "other-pump-device", serial: "SN4", name: "Left Return Pump" }],
};

async function openSyncPointWithOtherPumps(otherGroups) {
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass(
    { "sensor.pump_flow": { state: "300", attributes: {} } },
    {
      scheduleResponse: {
        points: [
          { time_minutes: 0, flags: 1, mode: "Sync", params: { MaxSpeed: 300, PhaseShift: 0, ParentSerial: "SN4" } },
        ],
      },
      wsResponse: { groups: [PUMP_GROUP, ...otherGroups] },
    },
  );
  await settled(el);
  el.shadowRoot.querySelector(".edit-button").click();
  await settled(el);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;
  return el;
}

test("parent-pump picker lists other pumps on the tank, excluding this device itself", async () => {
  const el = await openSyncPointWithOtherPumps([OTHER_PUMP_GROUP]);

  const select = [...el.shadowRoot.querySelectorAll(".param-label select")].find((s) =>
    [...s.options].some((o) => o.textContent.includes("Left Return Pump")),
  );
  assert.ok(select, "should show a combobox listing the other pump");

  const optionTexts = [...select.options].map((o) => o.textContent.trim());
  assert.deepEqual(optionTexts, ["Left Return Pump"]);
  // Confirms self-exclusion isn't accidental -- PUMP_GROUP's own
  // member ("Return Pump") must never appear as a choice for its own
  // ParentSerial field.
  assert.ok(!optionTexts.includes("Return Pump"));
});

test("parent-pump picker pre-selects the point's own current ParentSerial", async () => {
  const el = await openSyncPointWithOtherPumps([
    OTHER_PUMP_GROUP,
    { ...OTHER_PUMP_GROUP, members: [{ device_id: "third-pump", serial: "SN5", name: "Right Return Pump" }] },
  ]);

  const select = [...el.shadowRoot.querySelectorAll(".param-label select")].find((s) =>
    [...s.options].some((o) => o.value === "SN4"),
  );
  assert.equal(select.value, "SN4");
});

test("changing the parent-pump selection updates ParentSerial and saves correctly", async () => {
  const el = await openSyncPointWithOtherPumps([
    OTHER_PUMP_GROUP,
    { ...OTHER_PUMP_GROUP, members: [{ device_id: "third-pump", serial: "SN5", name: "Right Return Pump" }] },
  ]);

  const select = [...el.shadowRoot.querySelectorAll(".param-label select")].find((s) =>
    [...s.options].some((o) => o.value === "SN5"),
  );
  select.value = "SN5";
  select.dispatchEvent(new window.Event("change"));
  await el.updateComplete;

  el.shadowRoot.querySelector(".save-point-button").click();
  await el.updateComplete;

  assert.equal(el._schedulePoints[0].params.ParentSerial, "SN5");
});

test("parent-pump picker shows a clear message when no other pumps exist on the tank", async () => {
  const el = await openSyncPointWithOtherPumps([]);

  assert.ok(el.shadowRoot.textContent.includes("No other pumps found"));
  assert.equal(
    [...el.shadowRoot.querySelectorAll(".param-label select")].find((s) =>
      [...s.options].some((o) => o.value === "SN4" || o.value === "SN5"),
    ),
    undefined,
  );
});

// --------------------------------------------------------------------------
// Sync / Anti-Sync / EcoSmart Back -- the app's own real three choices
// --------------------------------------------------------------------------

test("the mode dropdown shows exactly Sync, Anti-Sync, and EcoSmart Back -- never a raw phase value", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;

  const options = [...el.shadowRoot.querySelector(".mode-label select").options].map((o) => o.textContent.trim());
  assert.ok(options.includes("Sync"));
  assert.ok(options.includes("Anti-Sync"));
  assert.ok(options.includes("EcoSmart Back"));
  // Exactly these three for the child-mode family -- not a fourth
  // "EcoSmartBack anti-phase" option, matching the real app.
  assert.equal(options.filter((o) => o.includes("Sync") || o.includes("EcoSmart")).length, 3);
});

test("a Sync point with PhaseShift 180 is correctly shown as Anti-Sync, not Sync", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "Sync", params: { MaxSpeed: 300, PhaseShift: 180, ParentSerial: "SN4" } },
  ]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;

  assert.equal(el.shadowRoot.querySelector(".mode-label select").value, "AntiSync");
});

test("a Sync point with PhaseShift 0 is correctly shown as plain Sync", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "Sync", params: { MaxSpeed: 300, PhaseShift: 0, ParentSerial: "SN4" } },
  ]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;

  assert.equal(el.shadowRoot.querySelector(".mode-label select").value, "Sync");
});

test("selecting Anti-Sync sets the real underlying mode to Sync with PhaseShift 180", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;

  const modeSelect = el.shadowRoot.querySelector(".mode-label select");
  modeSelect.value = "AntiSync";
  modeSelect.dispatchEvent(new window.Event("change"));
  await el.updateComplete;

  el.shadowRoot.querySelector(".save-point-button").click();
  await el.updateComplete;

  assert.equal(el._schedulePoints[0].mode, "Sync");
  assert.equal(el._schedulePoints[0].params.PhaseShift, 180);
});

test("selecting plain Sync sets PhaseShift to 0", async () => {
  const el = await openEditWithPoints([
    {
      time_minutes: 0,
      flags: 1,
      mode: "Sync",
      params: { MaxSpeed: 300, PhaseShift: 180, ParentSerial: "SN4" },
    },
  ]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;

  const modeSelect = el.shadowRoot.querySelector(".mode-label select");
  modeSelect.value = "Sync";
  modeSelect.dispatchEvent(new window.Event("change"));
  await el.updateComplete;

  el.shadowRoot.querySelector(".save-point-button").click();
  await el.updateComplete;

  assert.equal(el._schedulePoints[0].params.PhaseShift, 0);
});

test("selecting EcoSmart Back sets the real underlying mode to EcoSmartBack", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;

  const modeSelect = el.shadowRoot.querySelector(".mode-label select");
  modeSelect.value = "EcoSmartBack";
  modeSelect.dispatchEvent(new window.Event("change"));
  await el.updateComplete;

  el.shadowRoot.querySelector(".save-point-button").click();
  await el.updateComplete;

  assert.equal(el._schedulePoints[0].mode, "EcoSmartBack");
});

test("switching between Sync and Anti-Sync preserves MaxSpeed and the parent pump choice", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "Sync", params: { MaxSpeed: 450, PhaseShift: 0, ParentSerial: "SN4" } },
  ]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;

  const modeSelect = el.shadowRoot.querySelector(".mode-label select");
  modeSelect.value = "AntiSync";
  modeSelect.dispatchEvent(new window.Event("change"));
  await el.updateComplete;

  const maxSpeedInput = [...el.shadowRoot.querySelectorAll(".param-label input")].find(
    (i) => i.previousSibling?.textContent?.includes("MaxSpeed") || i.parentElement.textContent.includes("MaxSpeed"),
  );
  assert.equal(maxSpeedInput.value, "45");
  assert.equal(el._workingPoint.params.ParentSerial, "SN4");
});

// --------------------------------------------------------------------------
// Every real mode -- a full sweep confirming each one renders correctly
// --------------------------------------------------------------------------

// Every real mode this pump might support, and what its own edit form
// should show. Mirrors python-mobius's own PUMP_MODE_PARAMS exactly
// (BatteryBackup omitted -- supported_pump_modes() never offers it for
// any real pump, confirmed in python-mobius's own test suite, so the
// card's own mode dropdown never needs to render it at all).
const ALL_MODE_EXPECTATIONS = {
  ConstantSpeed: { params: { MaxSpeed: 300 }, fieldCount: 1, inputCount: 1 },
  Lagoon: { params: { MaxSpeed: 300 }, fieldCount: 1, inputCount: 1 },
  ReefCrest: { params: { MaxSpeed: 300 }, fieldCount: 1, inputCount: 1 },
  NutrientTransport: { params: { MaxSpeed: 300 }, fieldCount: 1, inputCount: 1 },
  TidalSwell: { params: { MaxSpeed: 300 }, fieldCount: 1, inputCount: 1 },
  Feed: { params: { MaxSpeed: 300 }, fieldCount: 1, inputCount: 1 },
  ShortPulse: { params: { MaxSpeed: 300, Time: 30 }, fieldCount: 2, inputCount: 2 },
  Gyre: { params: { MaxSpeed: 300, BigTime: 1000 }, fieldCount: 2, inputCount: 2 },
  Transition: { params: { RampType: "Linear" }, fieldCount: 1, inputCount: 0 },
  ExpandingPulse: { params: { MaxSpeed: 300, StartTime: 10, EndTime: 60 }, fieldCount: 3, inputCount: 3 },
  // Variance renders as a dropdown (categorical None/Low/Medium/High
  // label, confirmed from the app's own formatter), not an input --
  // only MinSpeed/MaxSpeed are inputs here.
  Random: { params: { MinSpeed: 100, MaxSpeed: 300, Variance: 50 }, fieldCount: 3, inputCount: 2 },
  Pulse: { params: { MaxSpeed: 300, OnTime: 5, OffTime: 10 }, fieldCount: 3, inputCount: 3 },
};

for (const [mode, expectation] of Object.entries(ALL_MODE_EXPECTATIONS)) {
  test(`${mode} renders its own edit form correctly (${expectation.fieldCount} field(s))`, async () => {
    const el = makeCard(PUMP_DEVICE_ID);
    const modeParams = Object.fromEntries(
      Object.keys(ALL_MODE_EXPECTATIONS).map((m) => [m, Object.keys(ALL_MODE_EXPECTATIONS[m].params)]),
    );
    el.hass = makePumpHass(
      { "sensor.pump_flow": { state: "300", attributes: {} } },
      {
        scheduleResponse: { points: [{ time_minutes: 0, flags: 1, mode, params: expectation.params }] },
        wsResponse: {
          groups: [{ ...PUMP_GROUP, modes: Object.keys(ALL_MODE_EXPECTATIONS), mode_params: modeParams }],
        },
      },
    );
    await settled(el);
    el.shadowRoot.querySelector(".edit-button").click();
    await settled(el);
    el.shadowRoot.querySelector(".point-row").click();
    await el.updateComplete;

    // Never crashes, never renders a raw PhaseShift field for anyone
    // (only Sync/EcoSmartBack have that param at all, neither is in
    // this per-mode sweep), and shows exactly the fields this mode's
    // own params call for.
    assert.equal(el.shadowRoot.querySelectorAll(".param-label").length, expectation.fieldCount);
    assert.equal(el.shadowRoot.querySelector(".mode-label select").value, mode);
    assert.equal(el.shadowRoot.querySelectorAll(".param-label input").length, expectation.inputCount);

    // Editing and saving works for every mode, not just the ones
    // already covered by earlier, more targeted tests.
    const inputs = el.shadowRoot.querySelectorAll(".param-label input");
    if (inputs.length > 0) {
      inputs[0].value = "999";
      inputs[0].dispatchEvent(new window.Event("change"));
      await el.updateComplete;
    }
    el.shadowRoot.querySelector(".save-point-button").click();
    await el.updateComplete;

    assert.equal(el.shadowRoot.querySelector(".point-edit-form"), null);
    assert.ok(el.shadowRoot.querySelector(".point-row").textContent.includes(mode));
  });
}

test("MaxSpeed/MinSpeed fields show the reverse-rotation hint; other params don't", async () => {
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass(
    { "sensor.pump_flow": { state: "300", attributes: {} } },
    {
      scheduleResponse: {
        points: [{ time_minutes: 0, flags: 1, mode: "Random", params: { MinSpeed: 100, MaxSpeed: 300, Variance: 50 } }],
      },
      wsResponse: {
        groups: [{ ...PUMP_GROUP, modes: ["Random"], mode_params: { Random: ["MinSpeed", "MaxSpeed", "Variance"] } }],
      },
    },
  );
  await settled(el);
  el.shadowRoot.querySelector(".edit-button").click();
  await settled(el);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;

  const labels = [...el.shadowRoot.querySelectorAll(".param-label")];
  const maxSpeedLabel = labels.find((l) => l.textContent.includes("MaxSpeed"));
  const varianceLabel = labels.find((l) => l.textContent.includes("Variance"));

  assert.ok(maxSpeedLabel.querySelector(".field-hint"));
  assert.equal(varianceLabel.querySelector(".field-hint"), null);
});

test("negative MaxSpeed round-trips correctly (encodes reverse rotation)", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;

  const input = el.shadowRoot.querySelector(".param-label input");
  input.value = "-30";
  input.dispatchEvent(new window.Event("change"));
  await el.updateComplete;
  el.shadowRoot.querySelector(".save-point-button").click();
  await el.updateComplete;

  assert.equal(el._schedulePoints[0].params.MaxSpeed, -300);
});

// --------------------------------------------------------------------------
// Saving the schedule to the real device
// --------------------------------------------------------------------------

test("Save schedule to device calls mobius.write_schedule_group with the right device_id and points", async () => {
  const points = [{ time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } }];
  const el = await openEditWithPoints(points);
  const calls = [];
  el.hass.callService = async (domain, service, data) => {
    calls.push({ domain, service, data });
  };

  el.shadowRoot.querySelector(".save-schedule-button").click();
  await el.updateComplete;
  await Promise.resolve();
  await el.updateComplete;

  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], {
    domain: "mobius",
    service: "write_schedule_group",
    data: { device_id: PUMP_DEVICE_ID, points },
  });
});

test("shows a success message after a successful save, which clears itself", async (t) => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  el.hass.callService = async () => {};

  t.mock.timers.enable({ apis: ["setTimeout"] });
  el.shadowRoot.querySelector(".save-schedule-button").click();
  await Promise.resolve();
  await Promise.resolve();
  await el.updateComplete;

  assert.ok(el.shadowRoot.querySelector(".save-schedule-success"));

  t.mock.timers.tick(4000);
  await el.updateComplete;

  assert.equal(el.shadowRoot.querySelector(".save-schedule-success"), null);
});

test("shows a clear error when the write itself fails, and does not falsely claim success", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  el.hass.callService = async () => {
    throw new Error("device did not acknowledge the write");
  };

  el.shadowRoot.querySelector(".save-schedule-button").click();
  await Promise.resolve();
  await Promise.resolve();
  await el.updateComplete;

  assert.ok(el.shadowRoot.textContent.includes("device did not acknowledge the write"));
  assert.equal(el.shadowRoot.querySelector(".save-schedule-success"), null);
});

test("the save-schedule button is disabled while a point is being actively edited", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;

  assert.ok(el.shadowRoot.querySelector(".save-schedule-button").disabled);
});

test("the save-schedule button is re-enabled once point editing is cancelled", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;
  el.shadowRoot.querySelector(".cancel-button").click();
  await el.updateComplete;

  assert.ok(!el.shadowRoot.querySelector(".save-schedule-button").disabled);
});

test("saving reflects locally-edited points, not just what was originally fetched", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;
  const input = el.shadowRoot.querySelector(".param-label input");
  input.value = "50";
  input.dispatchEvent(new window.Event("change"));
  await el.updateComplete;
  el.shadowRoot.querySelector(".save-point-button").click();
  await el.updateComplete;

  const calls = [];
  el.hass.callService = async (domain, service, data) => {
    calls.push(data);
  };
  el.shadowRoot.querySelector(".save-schedule-button").click();
  await Promise.resolve();
  await Promise.resolve();
  await el.updateComplete;

  assert.equal(calls[0].points[0].params.MaxSpeed, 500);
});

// --------------------------------------------------------------------------
// Light edit view -- channel sliders, reusing the same generic scaffolding
// --------------------------------------------------------------------------

async function openLightEditWithPoints(points) {
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeLightHass(
    { "sensor.left_royalblue": { state: "60", attributes: {} } },
    { "sensor.left_royalblue": [historyEntry(60, 0)] },
    { scheduleResponse: { points } },
  );
  await settled(el);
  el.shadowRoot.querySelector(".edit-button").click();
  await settled(el);
  return el;
}

test("clicking Edit schedule on a light group fetches the real schedule and shows the edit view", async () => {
  const el = await openLightEditWithPoints([
    { time_minutes: 0, flags: 1, channels: { RoyalBlue: 50, Violet: 20 } },
    { time_minutes: 480, flags: 3, channels: { RoyalBlue: 0, Violet: 0 } },
  ]);

  assert.equal(el.shadowRoot.querySelectorAll(".point-row").length, 2);
  assert.ok(el.shadowRoot.querySelector(".back-button"));
});

test("light point rows summarize with a mini colored bar chart, one bar per real channel", async () => {
  const el = await openLightEditWithPoints([{ time_minutes: 0, flags: 1, channels: { RoyalBlue: 50, Violet: 20 } }]);

  const bars = el.shadowRoot.querySelectorAll(".point-row .point-channel-bar");
  assert.equal(bars.length, 2);
  // Confirmed from the mockup: bar height and opacity both scale with
  // that channel's own intensity -- RoyalBlue (50%) should stand
  // taller and more opaque than Violet (20%).
  const royalBlueBar = [...bars].find((b) => b.title.startsWith("RoyalBlue"));
  const violetBar = [...bars].find((b) => b.title.startsWith("Violet"));
  assert.ok(parseFloat(royalBlueBar.style.height) > parseFloat(violetBar.style.height));
  assert.ok(parseFloat(royalBlueBar.style.opacity) > parseFloat(violetBar.style.opacity));
  // No pump-style mode summary ever shows for a light point.
  assert.equal(el.shadowRoot.querySelector(".point-row .point-mode"), null);
});

test("clicking a light point opens one slider per real channel, pre-filled with its own value", async () => {
  const el = await openLightEditWithPoints([{ time_minutes: 90, flags: 1, channels: { RoyalBlue: 45, Violet: 10 } }]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;

  const sliders = [...el.shadowRoot.querySelectorAll(".channel-slider-label")];
  assert.equal(sliders.length, 2);

  const royalBlueSlider = sliders.find((s) => s.textContent.includes("RoyalBlue"));
  const violetSlider = sliders.find((s) => s.textContent.includes("Violet"));
  assert.equal(royalBlueSlider.querySelector('input[type="range"]').value, "45");
  assert.equal(violetSlider.querySelector('input[type="range"]').value, "10");

  // Reuses the exact same time/period fields the pump form uses.
  assert.equal(el.shadowRoot.querySelector('input[type="time"]').value, "01:30");
});

test("adjusting a channel slider and saving updates that point's own channels", async () => {
  const el = await openLightEditWithPoints([{ time_minutes: 0, flags: 1, channels: { RoyalBlue: 45, Violet: 10 } }]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;

  const royalBlueSlider = [...el.shadowRoot.querySelectorAll(".channel-slider-label")]
    .find((s) => s.textContent.includes("RoyalBlue"))
    .querySelector('input[type="range"]');
  royalBlueSlider.value = "90";
  royalBlueSlider.dispatchEvent(new window.Event("input"));
  await el.updateComplete;

  el.shadowRoot.querySelector(".save-point-button").click();
  await el.updateComplete;

  assert.equal(el._schedulePoints[0].channels.RoyalBlue, 90);
  assert.equal(el._schedulePoints[0].channels.Violet, 10); // untouched channel survives
});

test("cancel discards light point edits without touching the original", async () => {
  const el = await openLightEditWithPoints([{ time_minutes: 0, flags: 1, channels: { RoyalBlue: 45, Violet: 10 } }]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;

  const slider = el.shadowRoot.querySelector('input[type="range"]');
  slider.value = "99";
  slider.dispatchEvent(new window.Event("input"));
  await el.updateComplete;

  el.shadowRoot.querySelector(".cancel-button").click();
  await el.updateComplete;

  assert.equal(el._schedulePoints[0].channels.RoyalBlue, 45);
});

test("Save schedule to device works identically for a light group", async () => {
  const points = [{ time_minutes: 0, flags: 1, channels: { RoyalBlue: 50, Violet: 20 } }];
  const el = await openLightEditWithPoints(points);
  const calls = [];
  el.hass.callService = async (domain, service, data) => calls.push({ domain, service, data });

  el.shadowRoot.querySelector(".save-schedule-button").click();
  await Promise.resolve();
  await Promise.resolve();
  await el.updateComplete;

  assert.deepEqual(calls[0], {
    domain: "mobius",
    service: "write_schedule_group",
    data: { device_id: LIGHT_DEVICE_ID, points },
  });
  assert.ok(el.shadowRoot.querySelector(".save-schedule-success"));
});

test("light edit view shows a clear error when the schedule fetch fails, with working back navigation", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeLightHass(
    { "sensor.left_royalblue": { state: "60", attributes: {} } },
    {},
    { scheduleError: new Error("relay connection lost") },
  );
  await settled(el);
  el.shadowRoot.querySelector(".edit-button").click();
  await settled(el);

  assert.ok(el.shadowRoot.textContent.includes("relay connection lost"));
  el.shadowRoot.querySelector(".back-button").click();
  await el.updateComplete;
  assert.ok(el.shadowRoot.querySelector(".edit-button"));
});

// --------------------------------------------------------------------------
// Adding and deleting points
// --------------------------------------------------------------------------

test("Add point (pump) opens a new point defaulted to the first supported mode", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);

  el.shadowRoot.querySelector(".add-point-button").click();
  await el.updateComplete;

  assert.ok(el.shadowRoot.querySelector(".point-edit-form"));
  assert.equal(el.shadowRoot.querySelector(".mode-label select").value, "ConstantSpeed");
});

test("saving a newly-added pump point adds it to the list", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  el.shadowRoot.querySelector(".add-point-button").click();
  await el.updateComplete;

  el.shadowRoot.querySelector(".save-point-button").click();
  await el.updateComplete;

  assert.equal(el._schedulePoints.length, 2);
  assert.equal(el.shadowRoot.querySelectorAll(".point-row").length, 2);
});

test("cancelling a freshly-added pump point removes it -- no half-configured point left behind", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  el.shadowRoot.querySelector(".add-point-button").click();
  await el.updateComplete;
  assert.equal(el._schedulePoints.length, 2, "Add itself already appends the point, before any confirmation");

  el.shadowRoot.querySelector(".cancel-button").click();
  await el.updateComplete;

  assert.equal(el._schedulePoints.length, 1, "cancelling the addition must remove it again");
  assert.equal(el.shadowRoot.querySelectorAll(".point-row").length, 1);
});

test("pressing Back while a freshly-added point is still mid-edit also removes it", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  el.shadowRoot.querySelector(".add-point-button").click();
  await el.updateComplete;

  el.shadowRoot.querySelector(".back-button").click();
  await el.updateComplete;

  assert.equal(el._schedulePoints.length, 1);
});

test("cancelling an edit to an EXISTING point does NOT remove it (only fresh additions get removed)", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;

  el.shadowRoot.querySelector(".cancel-button").click();
  await el.updateComplete;

  assert.equal(el._schedulePoints.length, 1);
});

test("Delete removes an existing point being edited", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
    { time_minutes: 480, flags: 3, mode: "Feed", params: { MaxSpeed: 200 } },
  ]);
  el.shadowRoot.querySelectorAll(".point-row")[0].click();
  await el.updateComplete;

  el.shadowRoot.querySelector(".delete-point-button").click();
  await el.updateComplete;

  assert.equal(el._schedulePoints.length, 1);
  assert.equal(el._schedulePoints[0].mode, "Feed");
});

test("Add point button is disabled while another point is being edited", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;

  assert.ok(el.shadowRoot.querySelector(".add-point-button").disabled);
});

test("Add point (light) opens a new point with every real channel defaulted to 0", async () => {
  const el = await openLightEditWithPoints([{ time_minutes: 0, flags: 1, channels: { RoyalBlue: 50, Violet: 20 } }]);

  el.shadowRoot.querySelector(".add-point-button").click();
  await el.updateComplete;

  const sliders = [...el.shadowRoot.querySelectorAll('.channel-slider-label input[type="range"]')];
  assert.equal(sliders.length, 2);
  assert.ok(sliders.every((s) => s.value === "0"));
});

test("cancelling a freshly-added light point removes it", async () => {
  const el = await openLightEditWithPoints([{ time_minutes: 0, flags: 1, channels: { RoyalBlue: 50, Violet: 20 } }]);
  el.shadowRoot.querySelector(".add-point-button").click();
  await el.updateComplete;
  assert.equal(el._schedulePoints.length, 2);

  el.shadowRoot.querySelector(".cancel-button").click();
  await el.updateComplete;

  assert.equal(el._schedulePoints.length, 1);
});

test("saving a newly-added light point adds it to the list with the edited channel values", async () => {
  const el = await openLightEditWithPoints([{ time_minutes: 0, flags: 1, channels: { RoyalBlue: 50, Violet: 20 } }]);
  el.shadowRoot.querySelector(".add-point-button").click();
  await el.updateComplete;

  const slider = el.shadowRoot.querySelector('.channel-slider-label input[type="range"]');
  slider.value = "77";
  slider.dispatchEvent(new window.Event("input"));
  await el.updateComplete;
  el.shadowRoot.querySelector(".save-point-button").click();
  await el.updateComplete;

  assert.equal(el._schedulePoints.length, 2);
  assert.equal(el._schedulePoints[1].channels.RoyalBlue, 77);
});

// --------------------------------------------------------------------------
// .mob export/import
// --------------------------------------------------------------------------

function makeMobFile(name, content) {
  return new window.File([typeof content === "string" ? content : JSON.stringify(content)], name, {
    type: "application/json",
  });
}

function selectMobFile(el, file) {
  const input = el.shadowRoot.querySelector(".mob-file-input");
  Object.defineProperty(input, "files", { value: [file], configurable: true });
  input.dispatchEvent(new window.Event("change"));
}

test("Download .mob calls export_schedule_group_mob with the right device_id", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  const calls = [];
  const originalCallWS = el.hass.callWS;
  el.hass.callWS = async (msg) => {
    if (msg.type === "mobius/export_schedule_group_mob") calls.push(msg);
    return originalCallWS(msg);
  };

  el.shadowRoot.querySelector('.header-icon-button[title="Download .mob"]').click();
  await Promise.resolve();
  await Promise.resolve();
  await el.updateComplete;

  assert.equal(calls.length, 1);
  assert.equal(calls[0].device_id, PUMP_DEVICE_ID);
  assert.equal(el.shadowRoot.querySelector(".save-schedule-error"), null);
});

test("Download .mob shows a clear error when the export itself fails", async () => {
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass(
    { "sensor.pump_flow": { state: "300", attributes: {} } },
    {
      scheduleResponse: { points: [] },
      exportMobError: new Error("device did not respond"),
    },
  );
  await settled(el);
  el.shadowRoot.querySelector(".edit-button").click();
  await settled(el);

  el.shadowRoot.querySelector('.header-icon-button[title="Download .mob"]').click();
  await Promise.resolve();
  await Promise.resolve();
  await el.updateComplete;

  assert.ok(el.shadowRoot.textContent.includes("device did not respond"));
});

test("Load .mob button triggers the hidden file input", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  const input = el.shadowRoot.querySelector(".mob-file-input");
  let clicked = false;
  input.addEventListener("click", () => {
    clicked = true;
  });

  el.shadowRoot.querySelector('.header-icon-button[title="Load .mob"]').click();
  assert.ok(clicked);
});

test("selecting a valid .mob file replaces the schedule with the parsed points", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  const newPoints = [
    { time_minutes: 60, flags: 1, mode: "Lagoon", params: { MaxSpeed: 400 } },
    { time_minutes: 720, flags: 3, mode: "Feed", params: { MaxSpeed: 200 } },
  ];
  const calls = [];
  const originalCallWS = el.hass.callWS;
  el.hass.callWS = async (msg) => {
    if (msg.type === "mobius/parse_schedule_mob") {
      calls.push(msg);
      return { points: newPoints };
    }
    return originalCallWS(msg);
  };

  selectMobFile(el, makeMobFile("schedule.mob", { schedules: [{ primitiveType: 4 }] }));
  await settled(el);

  assert.equal(calls.length, 1);
  assert.equal(calls[0].device_id, PUMP_DEVICE_ID);
  assert.deepEqual(calls[0].mob, { schedules: [{ primitiveType: 4 }] });
  assert.equal(el._schedulePoints.length, 2);
  assert.equal(el._schedulePoints[0].mode, "Lagoon");
});

test("a file with the wrong extension is rejected without ever calling parse_schedule_mob", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  let called = false;
  const originalCallWS = el.hass.callWS;
  el.hass.callWS = async (msg) => {
    if (msg.type === "mobius/parse_schedule_mob") called = true;
    return originalCallWS(msg);
  };

  selectMobFile(el, makeMobFile("schedule.json", { schedules: [] }));
  await Promise.resolve();
  await el.updateComplete;

  assert.equal(called, false);
  assert.ok(el.shadowRoot.textContent.includes(".mob file"));
  // The original schedule must survive an outright-rejected file untouched.
  assert.equal(el._schedulePoints.length, 1);
  assert.equal(el._schedulePoints[0].mode, "ConstantSpeed");
});

test("a .mob file with invalid JSON content shows a clear error, not a raw parse exception", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  let called = false;
  const originalCallWS = el.hass.callWS;
  el.hass.callWS = async (msg) => {
    if (msg.type === "mobius/parse_schedule_mob") called = true;
    return originalCallWS(msg);
  };

  selectMobFile(el, makeMobFile("schedule.mob", "{not valid json"));
  await settled(el);

  assert.equal(called, false);
  assert.ok(el.shadowRoot.textContent.toLowerCase().includes("valid"));
  assert.equal(el._schedulePoints.length, 1);
});

test("a real, valid .mob file the backend itself rejects shows the backend's own error", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  const el2Hass = el.hass;
  el2Hass.callWS = async (msg) => {
    if (msg.type === "mobius/parse_schedule_mob") {
      throw new Error("Couldn't parse this .mob file: wrong primitive type");
    }
    return { points: [] };
  };

  selectMobFile(el, makeMobFile("schedule.mob", { schedules: [{ primitiveType: 999 }] }));
  await settled(el);

  assert.ok(el.shadowRoot.textContent.includes("wrong primitive type"));
  assert.equal(el._schedulePoints.length, 1);
});

test("mob buttons are disabled while a point is being actively edited", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;

  const mobButtons = el.shadowRoot.querySelectorAll(".header-icon-button");
  assert.ok(mobButtons[0].disabled);
  assert.ok(mobButtons[1].disabled);
});

test(".mob import works identically for a light group", async () => {
  const el = await openLightEditWithPoints([{ time_minutes: 0, flags: 1, channels: { RoyalBlue: 50, Violet: 20 } }]);
  const newPoints = [{ time_minutes: 30, flags: 1, channels: { RoyalBlue: 80, Violet: 5 } }];
  el.hass.callWS = async (msg) => {
    if (msg.type === "mobius/parse_schedule_mob") return { points: newPoints };
    throw new Error(`unexpected: ${msg.type}`);
  };

  selectMobFile(el, makeMobFile("schedule.mob", { schedules: [{ primitiveType: 1 }] }));
  await settled(el);

  assert.equal(el._schedulePoints.length, 1);
  assert.equal(el._schedulePoints[0].channels.RoyalBlue, 80);
});

// --------------------------------------------------------------------------
// MaxSpeed/MinSpeed percentage display, Variance categorical display
// --------------------------------------------------------------------------

test("MaxSpeed displays as a rounded whole-number percentage, not the raw tenths value", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 427 } },
  ]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;

  // 427 raw -> round(427/10) = 43%, not 427 or 42.7.
  assert.equal(el.shadowRoot.querySelector(".param-label input").value, "43");
});

test("a negative MaxSpeed displays as a negative percentage, preserving the reverse-rotation sign", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: -300 } },
  ]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;

  assert.equal(el.shadowRoot.querySelector(".param-label input").value, "-30");
});

test("editing the MaxSpeed percentage stores the correctly-scaled raw tenths value", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;

  const input = el.shadowRoot.querySelector(".param-label input");
  input.value = "75";
  input.dispatchEvent(new window.Event("change"));
  await el.updateComplete;
  el.shadowRoot.querySelector(".save-point-button").click();
  await el.updateComplete;

  assert.equal(el._schedulePoints[0].params.MaxSpeed, 750);
});

test("Variance renders as a None/Low/Medium/High dropdown, not a number field", async () => {
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass(
    { "sensor.pump_flow": { state: "300", attributes: {} } },
    {
      scheduleResponse: {
        points: [
          { time_minutes: 0, flags: 1, mode: "Random", params: { MinSpeed: 100, MaxSpeed: 300, Variance: 550 } },
        ],
      },
      wsResponse: {
        groups: [{ ...PUMP_GROUP, modes: ["Random"], mode_params: { Random: ["MinSpeed", "MaxSpeed", "Variance"] } }],
      },
    },
  );
  await settled(el);
  el.shadowRoot.querySelector(".edit-button").click();
  await settled(el);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;

  const varianceLabel = [...el.shadowRoot.querySelectorAll(".param-label")].find((l) =>
    l.textContent.includes("Variance"),
  );
  const select = varianceLabel.querySelector("select");
  assert.ok(select, "Variance should render as a dropdown, not an input");
  assert.equal(select.querySelectorAll("option").length, 4);
  // 550 falls in the [400,700) "Medium" bucket.
  assert.equal(select.value, "Medium");
});

test("each Variance category boundary maps to the correct label", async () => {
  const cases = [
    [0, "None"],
    [1, "Low"],
    [399, "Low"],
    [400, "Medium"],
    [699, "Medium"],
    [700, "High"],
    [1000, "High"],
  ];
  for (const [raw, expectedLabel] of cases) {
    const el = makeCard(PUMP_DEVICE_ID);
    el.hass = makePumpHass(
      { "sensor.pump_flow": { state: "300", attributes: {} } },
      {
        scheduleResponse: {
          points: [
            { time_minutes: 0, flags: 1, mode: "Random", params: { MinSpeed: 100, MaxSpeed: 300, Variance: raw } },
          ],
        },
        wsResponse: {
          groups: [{ ...PUMP_GROUP, modes: ["Random"], mode_params: { Random: ["MinSpeed", "MaxSpeed", "Variance"] } }],
        },
      },
    );
    await settled(el);
    el.shadowRoot.querySelector(".edit-button").click();
    await settled(el);
    el.shadowRoot.querySelector(".point-row").click();
    await el.updateComplete;

    const varianceLabel = [...el.shadowRoot.querySelectorAll(".param-label")].find((l) =>
      l.textContent.includes("Variance"),
    );
    assert.equal(varianceLabel.querySelector("select").value, expectedLabel, `raw=${raw}`);
  }
});

test("choosing a Variance category and saving stores its own representative raw value", async () => {
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass(
    { "sensor.pump_flow": { state: "300", attributes: {} } },
    {
      scheduleResponse: {
        points: [{ time_minutes: 0, flags: 1, mode: "Random", params: { MinSpeed: 100, MaxSpeed: 300, Variance: 0 } }],
      },
      wsResponse: {
        groups: [{ ...PUMP_GROUP, modes: ["Random"], mode_params: { Random: ["MinSpeed", "MaxSpeed", "Variance"] } }],
      },
    },
  );
  await settled(el);
  el.shadowRoot.querySelector(".edit-button").click();
  await settled(el);
  el.shadowRoot.querySelector(".point-row").click();
  await el.updateComplete;

  const varianceLabel = [...el.shadowRoot.querySelectorAll(".param-label")].find((l) =>
    l.textContent.includes("Variance"),
  );
  const select = varianceLabel.querySelector("select");
  select.value = "High";
  select.dispatchEvent(new window.Event("change"));
  await el.updateComplete;
  el.shadowRoot.querySelector(".save-point-button").click();
  await el.updateComplete;

  assert.equal(el._schedulePoints[0].params.Variance, 850);
});

// --------------------------------------------------------------------------
// Chart hover tooltip and color legend
// --------------------------------------------------------------------------

test("chart shows a legend with each channel's own color and name, unconditionally", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeLightHass(
    { "sensor.left_royalblue": { state: "60", attributes: {} } },
    {
      "sensor.left_royalblue": [historyEntry(50, 0)],
      "sensor.left_violet": [historyEntry(30, 0)],
    },
  );
  await settled(el);

  const legendItems = [...el.shadowRoot.querySelectorAll(".chart-legend-item")];
  assert.equal(legendItems.length, 2);
  assert.ok(legendItems.some((i) => i.textContent.includes("RoyalBlue")));
  assert.ok(legendItems.some((i) => i.textContent.includes("Violet")));
  for (const item of legendItems) {
    assert.ok(item.querySelector(".chart-legend-swatch"));
  }
});

test("no hover tooltip is shown until the chart is actually hovered", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeLightHass(
    { "sensor.left_royalblue": { state: "60", attributes: {} } },
    { "sensor.left_royalblue": [historyEntry(50, 0)] },
  );
  await settled(el);

  assert.equal(el.shadowRoot.querySelector(".chart-tooltip"), null);
});

test("hovering the chart shows a tooltip with each channel's own value at that point in time", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeLightHass(
    { "sensor.left_royalblue": { state: "60", attributes: {} } },
    {
      "sensor.left_royalblue": [historyEntry(0, 0), historyEntry(80, 3600 * 12), historyEntry(60, 3600 * 20)],
      "sensor.left_violet": [historyEntry(10, 0), historyEntry(40, 3600 * 12)],
    },
  );
  await settled(el);

  // Simulate hovering at the exact midday point (0.5 of the day).
  el._chartHoverFraction = 0.5;
  await el.updateComplete;

  const tooltip = el.shadowRoot.querySelector(".chart-tooltip");
  assert.ok(tooltip);
  const rows = [...tooltip.querySelectorAll(".chart-tooltip-row")];
  assert.equal(rows.length, 2);

  const royalBlueRow = rows.find((r) => r.textContent.includes("RoyalBlue"));
  const violetRow = rows.find((r) => r.textContent.includes("Violet"));
  assert.ok(royalBlueRow.querySelector(".chart-tooltip-value").textContent.includes("80"));
  assert.ok(violetRow.querySelector(".chart-tooltip-value").textContent.includes("40"));
});

test("the hover line is positioned at the hovered fraction across the chart", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeLightHass(
    { "sensor.left_royalblue": { state: "60", attributes: {} } },
    { "sensor.left_royalblue": [historyEntry(50, 0)] },
  );
  await settled(el);

  el._chartHoverFraction = 0.75;
  await el.updateComplete;

  const line = el.shadowRoot.querySelector(".chart-hover-line");
  assert.ok(line.getAttribute("style").includes("75%"));
});

test("moving the mouse off the chart clears the tooltip", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeLightHass(
    { "sensor.left_royalblue": { state: "60", attributes: {} } },
    { "sensor.left_royalblue": [historyEntry(50, 0)] },
  );
  await settled(el);

  el._chartHoverFraction = 0.5;
  await el.updateComplete;
  assert.ok(el.shadowRoot.querySelector(".chart-tooltip"));

  el.shadowRoot.querySelector(".chart-container").dispatchEvent(new window.Event("mouseleave"));
  await el.updateComplete;

  assert.equal(el.shadowRoot.querySelector(".chart-tooltip"), null);
});

test("mousemove over the chart container converts pixel position to the correct hover fraction", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeLightHass(
    { "sensor.left_royalblue": { state: "60", attributes: {} } },
    { "sensor.left_royalblue": [historyEntry(50, 0)] },
  );
  await settled(el);

  const container = el.shadowRoot.querySelector(".chart-container");
  container.getBoundingClientRect = () => ({ left: 100, width: 400 });

  const event = new window.MouseEvent("mousemove", { clientX: 300 });
  Object.defineProperty(event, "currentTarget", { value: container });
  container.dispatchEvent(event);
  await el.updateComplete;

  // (300 - 100) / 400 = 0.5
  assert.equal(el._chartHoverFraction, 0.5);
});

// --------------------------------------------------------------------------
// Icon buttons (Back, Load .mob, Download .mob)
// --------------------------------------------------------------------------

test("the back button shows an icon, not text, with an accessible label", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);

  const backButton = el.shadowRoot.querySelector(".back-button");
  const icon = backButton.querySelector("ha-icon");
  assert.ok(icon);
  assert.equal(icon.getAttribute("icon"), "mdi:arrow-left");
  assert.equal(backButton.getAttribute("aria-label"), "Back");
});

test("Upload/Download .mob buttons show icons with accessible labels, positioned in the header", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);

  // Mockup order: Upload (Load) first, then Download -- both live in
  // the edit header itself, to the right of the title, not in a
  // separate row below the point list.
  const [loadButton, downloadButton] = el.shadowRoot.querySelectorAll(".edit-header .header-icon-button");
  assert.equal(loadButton.querySelector("ha-icon").getAttribute("icon"), "mdi:upload");
  assert.equal(loadButton.getAttribute("aria-label"), "Load .mob");
  assert.equal(downloadButton.querySelector("ha-icon").getAttribute("icon"), "mdi:download");
  assert.equal(downloadButton.getAttribute("aria-label"), "Download .mob");
});

test("Download .mob shows a spinning icon while exporting, not the plain download icon", async () => {
  const el = await openEditWithPoints([
    { time_minutes: 0, flags: 1, mode: "ConstantSpeed", params: { MaxSpeed: 300 } },
  ]);
  // Never resolves during this test -- keeps the export "in flight".
  el.hass.callWS = () => new Promise(() => {});

  const downloadButton = el.shadowRoot.querySelector('.header-icon-button[title="Download .mob"]');
  downloadButton.click();
  await el.updateComplete;

  const icon = downloadButton.querySelector("ha-icon");
  assert.equal(icon.getAttribute("icon"), "mdi:loading");
  assert.ok(icon.classList.contains("spin"));
});

// --------------------------------------------------------------------------
// Click pump reading -> HA's native more-info dialog (history, etc.)
// --------------------------------------------------------------------------

test("clicking the flow reading opens HA's native more-info dialog for that entity", async () => {
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass({
    "sensor.pump_flow": { state: "300", attributes: { unit_of_measurement: "GPH" } },
  });
  await settled(el);

  let firedDetail = null;
  el.addEventListener("hass-more-info", (e) => {
    firedDetail = e.detail;
  });

  el.shadowRoot.querySelector(".reading-clickable").click();

  assert.deepEqual(firedDetail, { entityId: "sensor.pump_flow" });
});

test("clicking the speed reading (fallback path) opens more-info for the speed entity", async () => {
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass({
    "sensor.pump_speed": { state: "42", attributes: {} },
  });
  await settled(el);

  let firedDetail = null;
  el.addEventListener("hass-more-info", (e) => {
    firedDetail = e.detail;
  });

  el.shadowRoot.querySelector(".reading-clickable").click();

  assert.deepEqual(firedDetail, { entityId: "sensor.pump_speed" });
});

test("the more-info event bubbles and crosses shadow DOM boundaries", async () => {
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass({
    "sensor.pump_flow": { state: "300", attributes: { unit_of_measurement: "GPH" } },
  });
  await settled(el);

  let firedOnDocument = false;
  document.addEventListener("hass-more-info", () => {
    firedOnDocument = true;
  });
  document.body.appendChild(el);

  el.shadowRoot.querySelector(".reading-clickable").click();

  assert.ok(firedOnDocument);
  el.remove();
});

test("pressing Enter on the reading also opens more-info (keyboard accessible)", async () => {
  const el = makeCard(PUMP_DEVICE_ID);
  el.hass = makePumpHass({
    "sensor.pump_flow": { state: "300", attributes: { unit_of_measurement: "GPH" } },
  });
  await settled(el);

  let fired = false;
  el.addEventListener("hass-more-info", () => {
    fired = true;
  });

  el.shadowRoot
    .querySelector(".reading-clickable")
    .dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter" }));

  assert.ok(fired);
});

// --------------------------------------------------------------------------
// Chart lines extend to "now" using the last known value (no new
// history point exists for a channel whose value hasn't changed,
// since HA never emits a state_changed event for a repeated value)
// --------------------------------------------------------------------------

function expectedNowX() {
  const startOfDay = new Date();
  startOfDay.setHours(0, 0, 0, 0);
  const dayMs = 24 * 60 * 60 * 1000;
  return ((Date.now() - startOfDay.getTime()) / dayMs) * 600; // CHART_WIDTH
}

test("a channel line extends to the current time using its last known value, not stopping where history ends", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  el.hass = makeLightHass(
    { "sensor.left_royalblue": { state: "60", attributes: {} } },
    // Last real point is at 8am -- far from "now" (this test runs at
    // whatever the actual wall-clock time is).
    { "sensor.left_royalblue": [historyEntry(0, 0), historyEntry(60, 3600 * 8)] },
  );
  await settled(el);

  const polyline = el.shadowRoot.querySelector(".chart polyline");
  const points = polyline.getAttribute("points").trim().split(" ");
  const lastPoint = points[points.length - 1];
  const [lastX, lastY] = lastPoint.split(",").map(Number);

  // Extended to "now" (within a couple of chart-width units to allow
  // for the few ms between the card's own Date.now() and this
  // assertion's), not left sitting at the 8am mark.
  assert.ok(Math.abs(lastX - expectedNowX()) < 2, `expected lastX near ${expectedNowX()}, got ${lastX}`);

  // Y position (thus value) is unchanged -- extending in time never
  // means inventing a different value, only holding the last real one.
  const secondToLastPoint = points[points.length - 2];
  const [, secondToLastY] = secondToLastPoint.split(",").map(Number);
  assert.equal(lastY, secondToLastY);
});

test("a channel that DID just report a value at the current moment isn't given a redundant duplicate point", async () => {
  const el = makeCard(LIGHT_DEVICE_ID);
  const nowSeconds = Math.floor((Date.now() - new Date().setHours(0, 0, 0, 0)) / 1000) + 60; // just in the future
  el.hass = makeLightHass(
    { "sensor.left_royalblue": { state: "60", attributes: {} } },
    { "sensor.left_royalblue": [historyEntry(0, 0), historyEntry(60, nowSeconds)] },
  );
  await settled(el);

  const polyline = el.shadowRoot.querySelector(".chart polyline");
  const points = polyline.getAttribute("points").trim().split(" ");
  // Only the two real points -- a "future" last point (relative to
  // Date.now()) must never get a synthetic point appended after it,
  // which would draw the line backwards.
  assert.equal(points.length, 2);
});
