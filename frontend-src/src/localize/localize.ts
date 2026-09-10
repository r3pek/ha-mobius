import * as en from "./languages/en.json";

/**
 * Adding a new language:
 *   1. Copy languages/en.json to languages/<code>.json and translate
 *      the values (keep every key identical).
 *   2. Import it here and add it to LANGUAGES below.
 */
const LANGUAGES: Record<string, unknown> = { en };

type Translations = typeof en;

function lookup(dict: unknown, dottedKey: string): string | undefined {
  const value = dottedKey
    .split(".")
    .reduce<unknown>(
      (obj, part) => (obj && typeof obj === "object" ? (obj as Record<string, unknown>)[part] : undefined),
      dict,
    );
  return typeof value === "string" ? value : undefined;
}

/**
 * Resolves a dotted key (e.g. "scene_card.title") against the
 * person's own language -- hass.locale.language when available,
 * otherwise the browser's own navigator.language, otherwise "en".
 * Falls back to the English string if the key is missing in the
 * resolved language, and to the key itself (visibly wrong, but never
 * a hard crash) if it's missing from English too.
 */
export function localize(key: keyof FlatKeys, language?: string | null): string {
  const lang = (language || (typeof navigator !== "undefined" ? navigator.language : "en")).split("-")[0].toLowerCase();
  const dict = LANGUAGES[lang] ?? en;
  return lookup(dict, key) ?? lookup(en, key) ?? key;
}

// A flattened "a.b" key type derived from en.json's own shape, purely
// so localize()'s own `key` parameter is type-checked against real,
// existing keys -- a typo is a compile error, not a silent runtime
// fallback to the raw key string.
type FlattenKeys<T, Prefix extends string = ""> = {
  [K in keyof T & string]: T[K] extends string
    ? `${Prefix}${K}`
    : T[K] extends Record<string, unknown>
      ? FlattenKeys<T[K], `${Prefix}${K}.`>
      : never;
}[keyof T & string];

type FlatKeys = { [K in FlattenKeys<Translations>]: unknown };
