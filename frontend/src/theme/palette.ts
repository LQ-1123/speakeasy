import { HONOR_META, HONOR_PALETTES, HONOR_SOURCE_COLOR } from "./honor-fixture";
import {
  THEME_ROLES,
  type ThemeColors,
  type ThemeMode,
  type ThemePalettePayload,
  type ThemeSnapshot,
} from "./types";

/* ------------------------------------------------------------------ colour maths */

export function normalizeHex(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const raw = value.trim().replace(/^#/, "");
  if (!/^[0-9a-fA-F]{3,8}$/.test(raw)) return null;
  const hex =
    raw.length === 3 || raw.length === 4
      ? raw
          .slice(0, 3)
          .split("")
          .map((c) => c + c)
          .join("")
      : raw.slice(0, 6);
  if (hex.length !== 6) return null;
  return `#${hex.toUpperCase()}`;
}

export function isHex(value: unknown): value is string {
  return normalizeHex(value) !== null;
}

function channel(hex: string, index: number): number {
  return parseInt(hex.replace("#", "").slice(index * 2, index * 2 + 2), 16);
}

export function rgb(hex: string): [number, number, number] {
  const normalized = normalizeHex(hex) ?? "#000000";
  return [channel(normalized, 0), channel(normalized, 1), channel(normalized, 2)];
}

export function srgbToLinear(value: number): number {
  const v = value / 255;
  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
}

export function relativeLuminance(hex: string): number {
  const [r, g, b] = rgb(hex).map(srgbToLinear);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

export function contrastRatio(foreground: string, background: string): number {
  const a = relativeLuminance(foreground);
  const b = relativeLuminance(background);
  const light = Math.max(a, b);
  const dark = Math.min(a, b);
  return (light + 0.05) / (dark + 0.05);
}

/** Flatten a translucent layer over an opaque background. */
export function compositeOver(foreground: string, background: string, alpha: number): string {
  const f = rgb(foreground);
  const b = rgb(background);
  const a = Math.max(0, Math.min(1, alpha));
  const out = f.map((v, i) => Math.round(v * a + b[i] * (1 - a)));
  return `#${out.map((v) => v.toString(16).padStart(2, "0")).join("")}`.toUpperCase();
}

export function mix(a: string, b: string, amount: number): string {
  const x = rgb(a);
  const y = rgb(b);
  const t = Math.max(0, Math.min(1, amount));
  return `#${x
    .map((v, i) => Math.round(v * (1 - t) + y[i] * t).toString(16).padStart(2, "0"))
    .join("")}`.toUpperCase();
}

export function withAlpha(hex: string, alpha: number): string {
  const [r, g, b] = rgb(hex);
  return `rgba(${r}, ${g}, ${b}, ${Math.max(0, Math.min(1, alpha)).toFixed(3)})`;
}

/* ------------------------------------------------------------------ palette helpers */

export function paletteFromPartial(
  partial: Partial<ThemeColors>,
  fallback: ThemeColors,
): ThemeColors {
  const out = { ...fallback };
  for (const role of THEME_ROLES) {
    const value = normalizeHex(partial[role]);
    if (value) out[role] = value;
  }
  return out;
}

export function palettePayloadToSnapshot(
  payload: ThemePalettePayload,
  revision: number,
  fallback = HONOR_PALETTES,
): ThemeSnapshot {
  const light = paletteFromPartial(payload.palettes.light, fallback.light);
  const dark = paletteFromPartial(payload.palettes.dark, fallback.dark);
  return makeSnapshot({
    revision,
    source: payload.source ?? "preview",
    label: payload.label ?? "导入色板",
    palettes: { light, dark },
    capabilities: {
      systemSemanticRoles: true,
      dynamicColor: true,
      haptics: false,
      transparency: true,
      reducedMotion: false,
      contrast: "standard",
    },
    meta: payload.meta,
  });
}

export function makeSnapshot(input: {
  revision: number;
  source: ThemeSnapshot["source"];
  label: string;
  palettes: { light: ThemeColors; dark: ThemeColors };
  capabilities?: Partial<ThemeSnapshot["capabilities"]>;
  meta?: ThemeSnapshot["meta"];
}): ThemeSnapshot {
  const snapshot: ThemeSnapshot = {
    schemaVersion: 1,
    revision: input.revision,
    source: input.source,
    label: input.label,
    palettes: input.palettes,
    capabilities: {
      systemSemanticRoles: false,
      dynamicColor: true,
      haptics: false,
      transparency: true,
      reducedMotion: false,
      contrast: "standard",
      ...input.capabilities,
    },
    contrast: { body: 4.5, large: 3, controls: 3 },
    fingerprint: fingerprintPalettes(input.palettes),
    meta: input.meta,
  };
  return snapshot;
}

export function fingerprintPalettes(palettes: {
  light: ThemeColors;
  dark: ThemeColors;
}): string {
  const text = [
    ...THEME_ROLES.map((role) => palettes.light[role]),
    ...THEME_ROLES.map((role) => palettes.dark[role]),
  ].join("|");
  let hash = 0x811c9dc5;
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  return `fp_${(hash >>> 0).toString(16).padStart(8, "0")}`;
}

export function honorSnapshot(revision = 1): ThemeSnapshot {
  return makeSnapshot({
    revision,
    source: "honor-500-pro-home-wallpaper",
    label: HONOR_META.label,
    palettes: HONOR_PALETTES,
    capabilities: {
      systemSemanticRoles: true,
      dynamicColor: true,
      haptics: false,
      transparency: true,
      reducedMotion: false,
      contrast: "standard",
    },
    meta: {
      device: `${HONOR_META.device.model} · ${HONOR_META.device.os}`,
      wallpaperSource: HONOR_META.wallpaperSource,
      capturedAt: HONOR_META.capturedAt,
      completeness: HONOR_META.completeness as "complete" | "partial",
      note: HONOR_META.note,
      sourceColor: HONOR_SOURCE_COLOR,
    },
  });
}

export function fallbackSnapshot(revision = 1): ThemeSnapshot {
  return makeSnapshot({
    revision,
    source: "web-fallback",
    label: "Sonder 静态主题",
    palettes: HONOR_PALETTES,
    capabilities: {
      systemSemanticRoles: false,
      dynamicColor: false,
      haptics: false,
      transparency: true,
      reducedMotion: false,
      contrast: "standard",
    },
  });
}

/* ------------------------------------------------------------------ validation */

export interface ContrastCheck {
  role: string;
  foreground: string;
  background: string;
  ratio: number;
  required: number;
  pass: boolean;
}

const PAIRS: Array<[keyof ThemeColors, keyof ThemeColors, number]> = [
  ["primary", "onPrimary", 4.5],
  ["onPrimary", "primary", 4.5],
  ["onPrimaryContainer", "primaryContainer", 4.5],
  ["onSecondaryContainer", "secondaryContainer", 4.5],
  ["onSurface", "surface", 4.5],
  ["onSurfaceVariant", "surface", 3],
  ["surface", "onSurface", 4.5],
  ["onError", "error", 4.5],
  ["onErrorContainer", "errorContainer", 4.5],
  ["outline", "surface", 3],
];

export function contrastChecks(colors: ThemeColors): ContrastCheck[] {
  return PAIRS.map(([fg, bg, required]) => {
    const ratio = contrastRatio(colors[fg], colors[bg]);
    return {
      role: `${fg}/${bg}`,
      foreground: colors[fg],
      background: colors[bg],
      ratio,
      required,
      pass: ratio + 1e-6 >= required,
    };
  });
}

export function paletteIsValid(colors: ThemeColors): boolean {
  return THEME_ROLES.every((role) => isHex(colors[role]));
}

export function validateSnapshot(input: unknown): { ok: boolean; errors: string[]; snapshot?: ThemeSnapshot } {
  const errors: string[] = [];
  if (!input || typeof input !== "object") return { ok: false, errors: ["theme snapshot must be an object"] };
  const raw = input as Partial<ThemeSnapshot>;
  if (raw.schemaVersion !== 1) errors.push("unsupported schemaVersion");
  if (!raw.palettes || !paletteIsValid(raw.palettes.light) || !paletteIsValid(raw.palettes.dark)) {
    errors.push("both light and dark palettes must contain every semantic role");
  }
  if (typeof raw.revision !== "number" || !Number.isFinite(raw.revision)) errors.push("revision must be finite");
  if (typeof raw.label !== "string" || !raw.label.trim()) errors.push("label is required");
  if (errors.length) return { ok: false, errors };
  const snapshot = raw as ThemeSnapshot;
  return { ok: true, errors: [], snapshot };
}

export function validatePalettePayload(input: unknown): { ok: boolean; errors: string[]; payload?: ThemePalettePayload } {
  if (!input || typeof input !== "object") return { ok: false, errors: ["palette payload must be an object"] };
  const raw = input as ThemePalettePayload;
  if (!raw.palettes?.light || !raw.palettes?.dark) return { ok: false, errors: ["light and dark palettes are required"] };
  for (const mode of ["light", "dark"] as const) {
    for (const [role, value] of Object.entries(raw.palettes[mode])) {
      if (!(THEME_ROLES as readonly string[]).includes(role)) return { ok: false, errors: [`unknown role ${role}`] };
      if (!isHex(value)) return { ok: false, errors: [`invalid colour ${role}=${String(value)}`] };
    }
  }
  return { ok: true, errors: [], payload: raw };
}

/* ------------------------------------------------------------------ active mode */

export function colorsFor(snapshot: ThemeSnapshot, mode: ThemeMode): ThemeColors {
  return snapshot.palettes[mode];
}
