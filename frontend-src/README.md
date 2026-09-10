# Mobius Lovelace cards

This directory (`frontend-src/`, at the repo root -- deliberately
**outside** `custom_components/mobius/`) holds everything needed to
build the integration's own Lovelace cards. `src/` is the only thing
anyone should ever hand-edit.

## Why the build output is never committed

`dist/` (under `../custom_components/mobius/frontend/`) is a generated
build artifact, produced from `src/` by `npm run build` -- but unlike
a typical "commit your build output" setup, **it is never checked
into git at all**, not even alongside `src/`.

This repo's own `hacs.json` sets `zip_release: true`, meaning HACS
installs from the release's own **zip asset**, not the raw git tree.
`.forgejo/workflows/release.yml` runs this project's own build as
part of creating that zip -- `dist/` is produced fresh, exactly once,
from whatever `src/` is at the tag being released, then baked
straight into the artifact HACS downloads. Since it's never persisted
in git between releases, there's no "forgot to rebuild before
committing" failure mode to guard against at all -- there's nothing
to go stale.

(A manual install, or CI's own test run, produces the same `dist/`
locally by running the build directly -- see below. Either way, the
file that ends up in `custom_components/mobius/frontend/dist/` is
never something a person hand-edits or commits.)

## Workflow

```
npm install     # once, after cloning
npm run build   # regenerates ../custom_components/mobius/frontend/dist/
npm test        # builds, then runs the real card in a jsdom DOM environment
```

Only `src/` (and this tooling itself) needs committing after a change
-- `npm test` already runs the build first, so CI (see
`.forgejo/workflows/test.yml`) catches a genuine compile failure the
same way a local `npm test` would.
