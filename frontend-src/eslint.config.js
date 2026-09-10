// @ts-check
import eslint from "@eslint/js";
import tseslint from "typescript-eslint";
import litPlugin from "eslint-plugin-lit";
import eslintConfigPrettier from "eslint-config-prettier";

export default tseslint.config(
  eslint.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["src/**/*.ts"],
    plugins: { lit: litPlugin },
    rules: {
      ...litPlugin.configs.recommended.rules,
      // Underscore-prefixed args are a deliberate "intentionally
      // unused" marker throughout this codebase (see
      // getStubConfig(_hass, entities) -- HA's own API requires the
      // parameter to exist even when a given implementation doesn't
      // need it).
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
    },
  },
  {
    files: ["test/**/*.mjs"],
    languageOptions: {
      globals: { console: "readonly", process: "readonly", window: "readonly", document: "readonly" },
    },
  },
  {
    ignores: ["node_modules/", "../custom_components/mobius/frontend/dist/"],
  },
  // Must be last -- turns off any stylistic rule Prettier itself
  // already enforces, so the two tools never disagree about the same
  // line of code.
  eslintConfigPrettier,
);
