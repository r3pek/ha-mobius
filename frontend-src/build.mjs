#!/usr/bin/env node
/**
 * Bundles every card in src/ into
 * ../custom_components/mobius/frontend/dist/ (the directory
 * frontend/__init__.py's own JSModuleRegistration actually serves,
 * and what .forgejo/workflows/release.yml bakes into the release
 * zip HACS installs from -- see this project's own README.md for
 * why dist/ itself is never committed to git at all).
 *
 * lit is bundled directly into each output file, not left as an
 * external/CDN import, so the served card has no runtime dependency
 * on anything other than what Home Assistant's own frontend already
 * provides.
 *
 * Usage:
 *   node build.mjs
 */

import * as esbuild from "esbuild";
import { readdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC_DIR = join(HERE, "src");
// Outside this directory entirely -- see this project's own README.md
// for why: only custom_components/mobius/frontend/dist/ ever reaches
// a real Home Assistant install (baked into the release zip at
// release time -- zip_release:true, see this repo's own hacs.json),
// while everything in THIS directory (frontend-src/ -- src/, test/,
// node_modules/, this build script itself) is a repository-only,
// developer-facing concern that never ships anywhere.
const DIST_DIR = join(HERE, "..", "custom_components", "mobius", "frontend", "dist");

const entryPoints = readdirSync(SRC_DIR)
  .filter((f) => f.endsWith(".js"))
  .map((f) => join(SRC_DIR, f));

if (entryPoints.length === 0) {
  console.error(`No .js files found in ${SRC_DIR}`);
  process.exit(1);
}

await esbuild.build({
  entryPoints,
  bundle: true,
  minify: true,
  format: "esm",
  // Explicit, not relying on esbuild's own current default (which
  // already happens to preserve native class syntax as of this
  // writing) -- Lit requires real ES6 class/extends semantics, and
  // downleveling them to ES5 function-based prototypes produces a
  // runtime "TypeError: Class constructor cannot be invoked without
  // 'new'" (a well-documented gotcha for exactly this kind of card;
  // see custom-cards/boilerplate-card's own README). Pinned so a
  // future esbuild version changing its own default can't silently
  // reintroduce that failure mode here.
  target: "es2022",
  outdir: DIST_DIR,
});

for (const f of entryPoints) console.log(`Built ${f.replace(HERE + "/", "")} -> dist/`);
