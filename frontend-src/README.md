# Mobius Lovelace cards

This directory (`frontend-src/`, at the repo root -- deliberately
**outside** `custom_components/mobius/`) holds everything needed to
build the integration's own Lovelace cards. `src/` is the only thing
anyone should ever hand-edit; the built output lands in
`custom_components/mobius/frontend/dist/`, which is never edited
directly, only produced from `src/` by the build.

## Why this lives outside custom_components/mobius/

This repo's own `hacs.json` sets `zip_release: false`, meaning HACS
installs directly from the tagged source tree -- whatever's under
`custom_components/mobius/` at that tag ships verbatim to every real
Home Assistant install. Keeping `src/`, `test/`, `node_modules/`, and
this build tooling itself in a sibling directory (rather than inside
`custom_components/mobius/frontend/` alongside `dist/`) means none of
that development-only material reaches an actual installation --
only `custom_components/mobius/frontend/dist/*.js` (what
`frontend/__init__.py`'s own `JSModuleRegistration` actually serves)
and `__init__.py` itself do.

## Workflow

```
npm install          # once, after cloning
npm run build        # after any change to src/ -- regenerates
                      # ../custom_components/mobius/frontend/dist/
npm test              # runs the real card in a jsdom DOM environment
npm run verify        # confirms dist/ actually matches what src/ would build right now
```

**Both `src/` (here) and the regenerated `dist/` (under
`custom_components/mobius/frontend/`) must be committed together.**
This repo has no build step at install time -- `dist/` has to already
be correct in the repository itself for a real install to work.

`npm run verify` exists specifically to catch the "edited `src/`,
forgot to run the build" mistake before it ships: it rebuilds in
memory and diffs the result against the committed `dist/`, failing
with a clear message (and the specific stale file) if they don't
match. This is wired into CI (see `.forgejo/workflows/test.yml`) --
a PR that edits `src/` without rebuilding `dist/` fails the build
there automatically, not just when someone remembers to run this by
hand.
