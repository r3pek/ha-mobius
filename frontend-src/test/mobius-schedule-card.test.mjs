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
  modes: ["ConstantSpeed", "Lagoon"],
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

function makeHass({ devices, wsResponse, wsError, historyResponse, states } = {}) {
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

function makePumpHass(states) {
  return makeHass({
    devices: { [PUMP_DEVICE_ID]: { id: PUMP_DEVICE_ID, via_device_id: TANK_DEVICE_ID } },
    states,
  });
}

function makeLightHass(states, historyResponse) {
  return makeHass({
    devices: { [LIGHT_DEVICE_ID]: { id: LIGHT_DEVICE_ID, via_device_id: TANK_DEVICE_ID } },
    states,
    historyResponse,
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
