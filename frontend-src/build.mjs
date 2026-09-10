#!/usr/bin/env node
/**
 * Bundles every card in src/ into dist/ (the directory
 * frontend/__init__.py's own JSModuleRegistration actually serves) --
 * lit is bundled directly into each output file, not left as an
 * external/CDN import, so the served card has no runtime dependency
 * on anything other than what Home Assistant's own frontend already
 * provides.
 *
 * Usage:
 *   node build.mjs            Rebuilds dist/ from src/.
 *   node build.mjs --verify   Rebuilds in memory (writes nothing) and
 *                             diffs the result against what's already
 *                             committed in dist/ -- exits non-zero and
 *                             prints which file(s) are stale if src/
 *                             was edited without re-running a real
 *                             build afterward. Intended for a CI step
 *                             or a pre-commit hook; catches exactly
 *                             the "edited src/, forgot to rebuild"
 *                             mistake this tooling exists to prevent.
 */

import * as esbuild from "esbuild";
import { readdirSync, readFileSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC_DIR = join(HERE, "src");
// Outside this directory entirely -- see this project's own README.md
// for why: only custom_components/mobius/frontend/dist/ actually ships
// to an end user's Home Assistant instance (HACS installs directly
// from the tagged source tree per this repo's own hacs.json
// zip_release:false, so whatever's under custom_components/mobius/
// goes to every install verbatim), while everything in THIS directory
// (frontend-src/ -- src/, test/, node_modules/, this build script
// itself) is a repository-only, developer-facing concern that should
// never reach a real installation at all.
const DIST_DIR = join(HERE, "..", "custom_components", "mobius", "frontend", "dist");
const VERIFY = process.argv.includes("--verify");

const entryPoints = readdirSync(SRC_DIR)
  .filter((f) => f.endsWith(".js"))
  .map((f) => join(SRC_DIR, f));

if (entryPoints.length === 0) {
  console.error(`No .js files found in ${SRC_DIR}`);
  process.exit(1);
}

const result = await esbuild.build({
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
  write: !VERIFY,
});

if (!VERIFY) {
  for (const f of entryPoints) console.log(`Built ${f.replace(HERE + "/", "")} -> dist/`);
  process.exit(0);
}

// --verify: esbuild's own write:false gives back in-memory
// outputFiles instead of touching disk -- diff each one's own
// contents against whatever's already committed in dist/.
let stale = [];
for (const file of result.outputFiles) {
  const distPath = file.path; // esbuild still resolves the intended on-disk path even with write:false
  const builtContent = file.text;
  if (!existsSync(distPath)) {
    stale.push(`${distPath} (missing entirely -- never built)`);
    continue;
  }
  const committedContent = readFileSync(distPath, "utf-8");
  if (committedContent !== builtContent) {
    stale.push(distPath);
  }
}

if (stale.length > 0) {
  console.error("dist/ is out of date with src/ -- run `npm run build` and commit the result:");
  for (const f of stale) console.error(`  ${f}`);
  process.exit(1);
}

console.log("dist/ matches what building src/ right now would produce.");
