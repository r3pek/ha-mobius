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
 * Resolves a dotted key (e.g. "scene_card.title") in `language`
 * (hass.locale.language), else the browser language, else English.
 * Missing keys fall back to English, then to the key itself.
 */
export function localize(key: keyof FlatKeys, language?: string | null): string {
  const lang = (language || (typeof navigator !== "undefined" ? navigator.language : "en")).split("-")[0].toLowerCase();
  const dict = LANGUAGES[lang] ?? en;
  return lookup(dict, key) ?? lookup(en, key) ?? key;
}

// Every dotted key of en.json, so an unknown key is a compile error.
type FlattenKeys<T, Prefix extends string = ""> = {
  [K in keyof T & string]: T[K] extends string
    ? `${Prefix}${K}`
    : T[K] extends Record<string, unknown>
      ? FlattenKeys<T[K], `${Prefix}${K}.`>
      : never;
}[keyof T & string];

type FlatKeys = { [K in FlattenKeys<Translations>]: unknown };
