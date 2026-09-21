# Mobius dashboard cards

Source of the integration's dashboard cards (schedule card and scene
card). Only `src/` is edited by hand.

The build output (`../custom_components/mobius/frontend/dist/`) is not
committed. Releases build it and include it in the zip that HACS installs
(`hacs.json` sets `zip_release: true`; see
`.forgejo/workflows/release.yml`). For a manual install from the
repository or for local testing, build it yourself:

```
npm install     # once
npm run build   # writes ../custom_components/mobius/frontend/dist/
npm test        # type check, lint, format check, build, then the card tests (jsdom)
```

Translations are in `src/localize/languages/`; to add a language, copy
`en.json` and register it in `src/localize/localize.ts`.
