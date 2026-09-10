/**
 * Executes the ACTUAL built dist/mobius-scene-card.js in a real jsdom
 * DOM environment -- not a mock of the card's own logic, the real
 * bundle a person's browser would load. Run with `npm test`.
 */

import { test, before } from "node:test";
import assert from "node:assert/strict";
import { JSDOM } from "jsdom";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const DIST_FILE = join(HERE, "..", "dist", "mobius-scene-card.js");

let ctor;

before(async () => {
  const dom = new JSDOM("<!DOCTYPE html><html><body></body></html>", {
    url: "http://localhost/",
    runScripts: "dangerously",
    pretendToBeVisual: true,
  });
  // Copy every enumerable global the jsdom window exposes, not a
  // hand-picked subset -- lit's own bundled code references several
  // DOM globals beyond the few obvious ones (Document, Node,
  // Element...), and missing any one of them fails at import time
  // with a confusing "X is not defined" that has nothing to do with
  // the card itself.
  for (const key of Object.getOwnPropertyNames(dom.window)) {
    if (!(key in globalThis)) {
      try { globalThis[key] = dom.window[key]; } catch { /* non-configurable global, skip */ }
    }
  }
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;

  await import(DIST_FILE);
  ctor = customElements.get("mobius-scene-card");
});

function makeHass(state, options, durationRemaining) {
  const calls = [];
  return {
    states: {
      "select.reef_tank_scene_selection": {
        entity_id: "select.reef_tank_scene_selection",
        state,
        attributes: { friendly_name: "Reef Tank Scene", options, duration_remaining_seconds: durationRemaining },
      },
    },
    callService: (domain, service, data) => calls.push({ domain, service, data }),
    _calls: calls,
  };
}

function makeCard(entity) {
  const el = document.createElement("mobius-scene-card");
  el.setConfig({ entity });
  document.body.appendChild(el);
  return el;
}

test("registers as a custom element", () => {
  assert.ok(ctor, "customElements.get('mobius-scene-card') should return the class");
});

test("registers itself in window.customCards", () => {
  const entry = window.customCards.find((c) => c.type === "mobius-scene-card");
  assert.ok(entry, "window.customCards should contain an entry for this card");
});

test("setConfig throws without an entity", () => {
  const el = document.createElement("mobius-scene-card");
  assert.throws(() => el.setConfig({}), /entity/i);
});

test("setConfig accepts a valid entity", () => {
  const el = document.createElement("mobius-scene-card");
  assert.doesNotThrow(() => el.setConfig({ entity: "select.reef_tank_scene_selection" }));
});

test("getStubConfig finds a scene entity from the entity list", () => {
  const stub = ctor.getStubConfig(null, ["sensor.foo", "select.reef_tank_scene_selection", "select.other"]);
  assert.equal(stub.entity, "select.reef_tank_scene_selection");
});

test("shows the normal-schedule message when no scene is active", async () => {
  const el = makeCard("select.reef_tank_scene_selection");
  el.hass = makeHass("None", ["None", "Feeding", "Storm Simulation"]);
  await el.updateComplete;
  const html = el.shadowRoot.innerHTML;
  assert.ok(html.includes("Running the normal schedule"));
  assert.equal(el.shadowRoot.querySelectorAll(".tile").length, 3);
});

test("marks the current option's own tile active", async () => {
  const el = makeCard("select.reef_tank_scene_selection");
  el.hass = makeHass("None", ["None", "Feeding"]);
  await el.updateComplete;
  const activeTiles = el.shadowRoot.querySelectorAll(".tile.active");
  assert.equal(activeTiles.length, 1);
  assert.ok(activeTiles[0].textContent.includes("Normal schedule"));
});

test("shows the active scene and its remaining duration", async () => {
  const el = makeCard("select.reef_tank_scene_selection");
  el.hass = makeHass("Feeding", ["None", "Feeding"], 125);
  await el.updateComplete;
  const html = el.shadowRoot.innerHTML;
  assert.ok(html.includes("Feeding active"));
  assert.ok(html.includes("2:05 remaining"));
});

test("omits the duration line when the attribute is absent", async () => {
  const el = makeCard("select.reef_tank_scene_selection");
  el.hass = makeHass("Feeding", ["None", "Feeding"], undefined);
  await el.updateComplete;
  assert.ok(!el.shadowRoot.innerHTML.includes("remaining"));
});

test("shows a clear warning for a missing/renamed entity", async () => {
  const el = makeCard("select.does_not_exist");
  el.hass = makeHass("None", ["None"]);
  await el.updateComplete;
  const html = el.shadowRoot.innerHTML;
  assert.ok(html.includes("Entity not found"));
  assert.ok(html.includes("select.does_not_exist"));
});

test("clicking a tile calls select.select_option with the right option", async () => {
  const el = makeCard("select.reef_tank_scene_selection");
  const hass = makeHass("None", ["None", "Feeding"]);
  el.hass = hass;
  await el.updateComplete;
  const tiles = el.shadowRoot.querySelectorAll(".tile");
  tiles[1].click(); // "Feeding"
  assert.equal(hass._calls.length, 1);
  assert.deepEqual(hass._calls[0], {
    domain: "select", service: "select_option",
    data: { entity_id: "select.reef_tank_scene_selection", option: "Feeding" },
  });
});

test("clicking the active tile still re-issues the same option (idempotent, not a no-op guard)", async () => {
  const el = makeCard("select.reef_tank_scene_selection");
  const hass = makeHass("Feeding", ["None", "Feeding"]);
  el.hass = hass;
  await el.updateComplete;
  const activeTile = el.shadowRoot.querySelector(".tile.active");
  activeTile.click();
  assert.equal(hass._calls[0].data.option, "Feeding");
});
