/**
 * Seed → complete Material 3 palette.
 *
 * Android 12+ only exposes a single wallpaper accent colour through the legacy
 * `getTheme()` bridge (`accent` / `onAccent`). Reusing that accent for `primary`
 * alone leaves every container role (bubbles, tab pill, tags, avatar wells,
 * notification banner …) stuck on the old Honor light-blue fixture.
 *
 * This module derives the *whole* semantic palette from the system accent with
 * the same Material Color Utilities algorithms Android's dynamic colour uses:
 * `Hct` + `SchemeTonalSpot` build the TonalPalettes, and `MaterialDynamicColors`
 * applies the M3 semantic tone mapping (primary 40/80, onPrimary 100/20,
 * primaryContainer 90/30, onPrimaryContainer 10/90, surfaces 98/6, …).
 *
 * It is a synchronous, DOM-free pure function: no workers, no async, no I/O.
 */
import {
  Hct,
  MaterialDynamicColors,
  SchemeTonalSpot,
  argbFromHex,
  hexFromArgb,
  type DynamicColor,
} from "@material/material-color-utilities";

import { normalizeHex } from "./palette";
import { THEME_ROLES, type ThemeColors, type ThemeMode } from "./types";

/**
 * Semantic role → MaterialDynamicColors accessor. Each accessor resolves the
 * spec-mandated tone for the active scheme, so a single seed reproduces the
 * complete light *and* dark pair — not just `primary`.
 */
const ROLE_ACCESSORS = {
  primary: "primary",
  onPrimary: "onPrimary",
  primaryContainer: "primaryContainer",
  onPrimaryContainer: "onPrimaryContainer",
  secondary: "secondary",
  onSecondary: "onSecondary",
  secondaryContainer: "secondaryContainer",
  onSecondaryContainer: "onSecondaryContainer",
  tertiary: "tertiary",
  onTertiary: "onTertiary",
  tertiaryContainer: "tertiaryContainer",
  onTertiaryContainer: "onTertiaryContainer",
  error: "error",
  onError: "onError",
  errorContainer: "errorContainer",
  onErrorContainer: "onErrorContainer",
  surface: "surface",
  onSurface: "onSurface",
  surfaceVariant: "surfaceVariant",
  onSurfaceVariant: "onSurfaceVariant",
  surfaceDim: "surfaceDim",
  surfaceBright: "surfaceBright",
  surfaceContainerLowest: "surfaceContainerLowest",
  surfaceContainerLow: "surfaceContainerLow",
  surfaceContainer: "surfaceContainer",
  surfaceContainerHigh: "surfaceContainerHigh",
  surfaceContainerHighest: "surfaceContainerHighest",
  outline: "outline",
  outlineVariant: "outlineVariant",
  inverseSurface: "inverseSurface",
  inverseOnSurface: "inverseOnSurface",
  inversePrimary: "inversePrimary",
  scrim: "scrim",
  shadow: "shadow",
  surfaceTint: "surfaceTint",
} as const satisfies Record<keyof ThemeColors, keyof MaterialDynamicColors>;

/** Stateless accessor holder; MaterialDynamicColors methods only build values. */
const dynamicColors = new MaterialDynamicColors();

const CONTRAST_MIN = -1;
const CONTRAST_MAX = 1;

/** M3 contrast level: -1 = minimum, 0 = standard, 1 = maximum. */
export function clampContrastLevel(level: number): number {
  if (!Number.isFinite(level)) return 0;
  return Math.min(CONTRAST_MAX, Math.max(CONTRAST_MIN, level));
}

/** True when the bridge value can be used as a seed colour. */
export function isSeedColor(value: unknown): value is string {
  return normalizeHex(value) !== null;
}

/**
 * Derive the M3 palette for one mode from a seed colour.
 *
 * `mode` selects the light or dark scheme; the same seed therefore yields the
 * harmonised pair the ThemeProvider swaps between. Surface roles stay near-white
 * (light, tone 98) / near-black (dark, tone 6) neutrals so white cards keep
 * working, while every accent *container* hue follows the seed.
 *
 * @throws RangeError when the seed is not a hex colour.
 */
export function paletteFromSeed(seedHex: string, mode: ThemeMode, contrast = 0): ThemeColors {
  const seed = normalizeHex(seedHex);
  if (!seed) throw new RangeError(`paletteFromSeed: invalid seed colour "${String(seedHex)}"`);

  // Pin the 2021 M3 spec explicitly: it is the tone table Android 12/13 dynamic
  // colour uses (primary 40/80, containers 90/30, surfaces 98/6 …). Explicit is
  // safer than trusting the library default to stay stable across upgrades.
  const scheme = new SchemeTonalSpot(
    Hct.fromInt(argbFromHex(seed)),
    mode === "dark",
    clampContrastLevel(contrast),
    "2021",
  );
  const colors = {} as ThemeColors;
  for (const role of THEME_ROLES) {
    const accessor = ROLE_ACCESSORS[role];
    const build = dynamicColors[accessor] as unknown as () => DynamicColor;
    colors[role] = hexFromArgb(build().getArgb(scheme)).toUpperCase();
  }
  return colors;
}

/** Derive the complete light + dark pair from one system accent colour. */
export function palettesFromSeed(
  seedHex: string,
  contrast = 0,
): { light: ThemeColors; dark: ThemeColors } {
  // Validate once up front so an invalid seed fails before allocating a scheme.
  if (!isSeedColor(seedHex)) {
    throw new RangeError(`palettesFromSeed: invalid seed colour "${String(seedHex)}"`);
  }
  return {
    light: paletteFromSeed(seedHex, "light", contrast),
    dark: paletteFromSeed(seedHex, "dark", contrast),
  };
}
