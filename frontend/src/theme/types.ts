/**
 * SpeakEasy dynamic theme contract.
 *
 * The theme pipeline has three layers:
 *  1. source adapters (Android system, Honor palette fixture, local image, web fallback)
 *  2. a validated ThemeSnapshot (the only shape components ever see)
 *  3. semantic CSS custom properties published by ThemeProvider
 *
 * Components never consume a raw accent colour. A colour role is always used as
 * an explicit pair (e.g. primaryContainer + onPrimaryContainer).
 */

export type ThemeMode = "light" | "dark";
export type ThemePreference = "system" | ThemeMode;

export type ThemeSource =
  | "android-system"
  | "honor-500-pro-home-wallpaper"
  | "web-fallback"
  | "image"
  | "preview";

export type ContrastLevel = "standard" | "medium" | "high";

export interface ThemeColors {
  primary: string;
  onPrimary: string;
  primaryContainer: string;
  onPrimaryContainer: string;
  secondary: string;
  onSecondary: string;
  secondaryContainer: string;
  onSecondaryContainer: string;
  tertiary: string;
  onTertiary: string;
  tertiaryContainer: string;
  onTertiaryContainer: string;
  error: string;
  onError: string;
  errorContainer: string;
  onErrorContainer: string;
  surface: string;
  onSurface: string;
  surfaceVariant: string;
  onSurfaceVariant: string;
  surfaceDim: string;
  surfaceBright: string;
  surfaceContainerLowest: string;
  surfaceContainerLow: string;
  surfaceContainer: string;
  surfaceContainerHigh: string;
  surfaceContainerHighest: string;
  outline: string;
  outlineVariant: string;
  inverseSurface: string;
  inverseOnSurface: string;
  inversePrimary: string;
  scrim: string;
  shadow: string;
  surfaceTint: string;
}

export interface ThemeCapabilities {
  /** True only when the source actually supplied the complete Material semantic palette. */
  systemSemanticRoles: boolean;
  dynamicColor: boolean;
  haptics: boolean;
  transparency: boolean;
  reducedMotion: boolean;
  contrast: ContrastLevel;
}

export interface ThemeContrast {
  /** Target ratios used for validation / documentation. */
  body: number;
  large: number;
  controls: number;
}

export interface ThemeSnapshot {
  schemaVersion: 1;
  revision: number;
  source: ThemeSource;
  label: string;
  /** Palette set. The active mode is selected by ThemeProvider. */
  palettes: { light: ThemeColors; dark: ThemeColors };
  capabilities: ThemeCapabilities;
  contrast: ThemeContrast;
  fingerprint: string;
  /** Never sent to a server. Only kept locally / shown in the preview panel. */
  meta?: {
    device?: string;
    wallpaperSource?: string;
    capturedAt?: string;
    completeness?: "complete" | "partial";
    note?: string;
    sourceColor?: string;
  };
}

export interface ThemeSettings {
  preference: ThemePreference;
  transparency: boolean;
  haptics: boolean;
  reducedMotion: boolean;
}

export interface ThemeState {
  snapshot: ThemeSnapshot;
  mode: ThemeMode;
  settings: ThemeSettings;
}

export interface ThemePalettePayload {
  label?: string;
  source?: ThemeSource;
  sourceColor?: string;
  palettes: { light: Partial<ThemeColors>; dark: Partial<ThemeColors> };
  meta?: ThemeSnapshot["meta"];
}

export const THEME_ROLES = [
  "primary",
  "onPrimary",
  "primaryContainer",
  "onPrimaryContainer",
  "secondary",
  "onSecondary",
  "secondaryContainer",
  "onSecondaryContainer",
  "tertiary",
  "onTertiary",
  "tertiaryContainer",
  "onTertiaryContainer",
  "error",
  "onError",
  "errorContainer",
  "onErrorContainer",
  "surface",
  "onSurface",
  "surfaceVariant",
  "onSurfaceVariant",
  "surfaceDim",
  "surfaceBright",
  "surfaceContainerLowest",
  "surfaceContainerLow",
  "surfaceContainer",
  "surfaceContainerHigh",
  "surfaceContainerHighest",
  "outline",
  "outlineVariant",
  "inverseSurface",
  "inverseOnSurface",
  "inversePrimary",
  "scrim",
  "shadow",
  "surfaceTint",
] as const satisfies ReadonlyArray<keyof ThemeColors>;

export type ThemeRole = (typeof THEME_ROLES)[number];
