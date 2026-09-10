# Mobius Lovelace cards

`src/` is the only thing anyone should ever hand-edit. `dist/` is a
generated build artifact -- it's what `frontend/__init__.py`'s own
`JSModuleRegistration` actually serves to a browser, but it is never
edited directly, only produced from `src/` by the build.

## Workflow

```
npm install        # once, after cloning
npm run build       # after any change to src/ -- regenerates dist/
npm test            # runs the real card in a jsdom DOM environment
npm run verify       # confirms dist/ actually matches what src/ would build right now
```

**Both `src/` and the regenerated `dist/` must be committed together.**
This repo has no build step at install time (HACS/manual installs just
copy the `custom_components/mobius` folder as-is, with no npm/node
involved on the end user's machine at all) -- `dist/` has to already
be correct in the repository itself for a real install to work.

`npm run verify` exists specifically to catch the "edited `src/`,
forgot to run the build" mistake before it ships: it rebuilds in
memory and diffs the result against the committed `dist/`, failing
with a clear message (and the specific stale file) if they don't
match. Wire it into a pre-commit hook or CI step if you want it
enforced automatically rather than run by hand.
