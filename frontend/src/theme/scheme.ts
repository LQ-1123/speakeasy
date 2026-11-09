import {
  Hct,
  MaterialDynamicColors,
  SchemeTonalSpot,
  argbFromHex,
  hexFromArgb,
} from "@material/material-color-utilities";

import type { ThemeColors } from "./types";

const METHODS = {
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
} as const satisfies Record<keyof ThemeColors, string>;

export function paletteFromSource(sourceHex: string, dark: boolean, contrast = 0): ThemeColors {
  const scheme = new SchemeTonalSpot(Hct.fromInt(argbFromHex(sourceHex)), dark, contrast);
  const dynamic = new MaterialDynamicColors();
  const colors = {} as ThemeColors;
  for (const [role, method] of Object.entries(METHODS) as Array<
    [keyof ThemeColors, keyof MaterialDynamicColors]
  >) {
    const callback = dynamic[method] as unknown as () => { getArgb: (s: SchemeTonalSpot) => number };
    colors[role] = hexFromArgb(callback().getArgb(scheme));
  }
  return colors;
}

export function palettesFromSource(sourceHex: string, contrast = 0) {
  return {
    light: paletteFromSource(sourceHex, false, contrast),
    dark: paletteFromSource(sourceHex, true, contrast),
  };
}
