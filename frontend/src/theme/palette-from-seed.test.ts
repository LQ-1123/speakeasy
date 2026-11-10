import { describe, expect, it } from "vitest";

import { Hct, argbFromHex } from "@material/material-color-utilities";

import { HONOR_PALETTES } from "./honor-fixture";
import { contrastChecks, relativeLuminance } from "./palette";
import {
  clampContrastLevel,
  isSeedColor,
  paletteFromSeed,
  palettesFromSeed,
} from "./palette-from-seed";
import { THEME_ROLES, type ThemeMode } from "./types";

/** Samsung/Honor style warm-brown wallpaper accent (真机截图里的壁纸棕). */
const BROWN = "#8C4A2F";
/** Second seed proves the palette is not tuned to one colour. */
const GREEN = "#2E7D32";
/** The old Honor light-blue primary container that leaked into every surface. */
const HONOR_CONTAINER = "#B9CBFF";

function hueOf(hex: string): number {
  return Hct.fromInt(argbFromHex(hex)).hue;
}

/** Shortest angular distance between two HCT hues, in degrees. */
function hueDistance(first: number, second: number): number {
  const diff = Math.abs(first - second) % 360;
  return Math.min(diff, 360 - diff);
}

function toneOf(hex: string): number {
  return Hct.fromInt(argbFromHex(hex)).tone;
}

function paletteFor(seed: string, mode: ThemeMode) {
  const palettes = palettesFromSeed(seed);
  return mode === "light" ? palettes.light : palettes.dark;
}

describe("paletteFromSeed", () => {
  it("fills every semantic role with a legal hex colour in both modes", () => {
    for (const seed of [BROWN, GREEN]) {
      const palettes = palettesFromSeed(seed);
      for (const mode of ["light", "dark"] as const) {
        const colors = palettes[mode];
        expect(Object.keys(colors).sort()).toEqual([...THEME_ROLES].sort());
        for (const role of THEME_ROLES) {
          expect(colors[role], `${seed} ${mode} ${role}`).toMatch(/^#[0-9A-F]{6}$/);
        }
      }
      // Pure/deterministic: the same accent always yields the same pair.
      expect(palettesFromSeed(seed)).toEqual(palettes);
    }
    // Light and dark are independent schemes, not copies.
    const palettes = palettesFromSeed(BROWN);
    expect(palettes.dark.primary).not.toBe(palettes.light.primary);
    expect(palettes.dark.surface).not.toBe(palettes.light.surface);
  });

  it("moves container roles off the Honor light blue onto the wallpaper hue", () => {
    const light = paletteFor(BROWN, "light");
    const dark = paletteFor(BROWN, "dark");

    // The exact reported bug: primaryContainer used to stay #B9CBFF after the
    // wallpaper turned brown, so bubbles / pills / tags stayed blue.
    for (const colors of [light, dark]) {
      expect(colors.primaryContainer).not.toBe(HONOR_CONTAINER);
      expect(hueDistance(hueOf(colors.primaryContainer), hueOf(HONOR_CONTAINER))).toBeGreaterThan(90);
      // Secondary containers must also leave the Honor blue family.
      expect(hueDistance(hueOf(colors.secondaryContainer), hueOf(HONOR_CONTAINER))).toBeGreaterThan(90);
    }
    for (const colors of [light, dark]) {
      expect(hueDistance(hueOf(colors.primaryContainer), hueOf(BROWN))).toBeLessThan(20);
    }
    // Every wallpaper-derived accent/container role follows the seed hue.
    for (const role of [
      "primary",
      "primaryContainer",
      "secondary",
      "secondaryContainer",
      "surfaceTint",
    ] as const) {
      expect(hueDistance(hueOf(light[role]), hueOf(BROWN)), role).toBeLessThan(45);
      expect(hueDistance(hueOf(dark[role]), hueOf(BROWN)), role).toBeLessThan(45);
    }

    // A green wallpaper accent produces green containers instead.
    const green = palettesFromSeed(GREEN);
    expect(hueDistance(hueOf(green.light.primaryContainer), hueOf(GREEN))).toBeLessThan(20);
    expect(green.light.primaryContainer).not.toBe(light.primaryContainer);
    expect(hueDistance(hueOf(light.primaryContainer), hueOf(green.light.primaryContainer))).toBeGreaterThan(45);
  });

  it("keeps the M3 semantic tone mapping and neutral near-white / near-black surfaces", () => {
    const light = paletteFor(BROWN, "light");
    const dark = paletteFor(BROWN, "dark");

    // M3 (ColorSpec 2021) accent tones: primary 40/80, onPrimary 100/20,
    // primaryContainer 90/30, onPrimaryContainer 30/90 — the exact mapping in
    // material-color-utilities 0.4.0, which keeps the paired contrast ≥ 4.5:1.
    expect(toneOf(light.primary)).toBeCloseTo(40, 0);
    expect(toneOf(dark.primary)).toBeCloseTo(80, 0);
    expect(toneOf(light.onPrimary)).toBeCloseTo(100, 0);
    expect(toneOf(dark.onPrimary)).toBeCloseTo(20, 0);
    expect(toneOf(light.primaryContainer)).toBeCloseTo(90, 0);
    expect(toneOf(dark.primaryContainer)).toBeCloseTo(30, 0);
    expect(toneOf(light.onPrimaryContainer)).toBeCloseTo(30, 0);
    expect(toneOf(dark.onPrimaryContainer)).toBeCloseTo(90, 0);

    // Surfaces stay near-white / near-black low-chroma neutrals: only the
    // accent containers should carry the wallpaper colour.
    expect(relativeLuminance(light.surface)).toBeGreaterThan(0.85);
    expect(relativeLuminance(dark.surface)).toBeLessThan(0.05);
    for (const colors of [light, dark]) {
      for (const role of ["surface", "surfaceContainerLow", "outlineVariant"] as const) {
        expect(Hct.fromInt(argbFromHex(colors[role])).chroma, role).toBeLessThan(12);
      }
    }
  });

  it("passes palette.contrastChecks for brown and green seeds in both modes", () => {
    for (const seed of [BROWN, GREEN]) {
      for (const mode of ["light", "dark"] as const) {
        const checks = contrastChecks(paletteFor(seed, mode));
        for (const check of checks) {
          expect(check.pass, `${seed} ${mode} ${check.role} = ${check.ratio.toFixed(2)}:1`).toBe(true);
        }
      }
    }
  });

  it("derives the complete light + dark pair well inside the 20ms budget", () => {
    palettesFromSeed(BROWN); // warm up modules/JIT
    const runs: number[] = [];
    for (let i = 0; i < 10; i += 1) {
      const started = performance.now();
      palettesFromSeed(BROWN);
      runs.push(performance.now() - started);
    }
    const average = runs.reduce((sum, value) => sum + value, 0) / runs.length;
    expect(Math.min(...runs)).toBeLessThan(20);
    expect(average).toBeLessThan(20);
    // `paletteFromSeed` is synchronous and works for a single mode too.
    expect(paletteFromSeed(BROWN, "light").primary).toBe(paletteFor(BROWN, "light").primary);
  });

  it("rejects invalid seeds and clamps the contrast level", () => {
    expect(isSeedColor(BROWN)).toBe(true);
    expect(isSeedColor("#abc")).toBe(true);
    expect(isSeedColor("#GGGGGG")).toBe(false);
    expect(isSeedColor(undefined)).toBe(false);
    expect(() => palettesFromSeed("not-a-colour")).toThrow(RangeError);
    expect(() => paletteFromSeed("", "light")).toThrow(RangeError);

    expect(clampContrastLevel(5)).toBe(1);
    expect(clampContrastLevel(-9)).toBe(-1);
    expect(clampContrastLevel(Number.NaN)).toBe(0);

    // High-contrast mode still produces a complete, legal, readable palette.
    const highContrast = paletteFor(BROWN, "light");
    expect(paletteFromSeed(BROWN, "light", 1)).not.toEqual(highContrast);
    for (const check of contrastChecks(paletteFromSeed(BROWN, "light", 1))) {
      expect(check.pass, check.role).toBe(true);
    }
    // The static fixture remains untouched (it is still the pre-hydration/web fallback).
    expect(HONOR_PALETTES.light.primaryContainer).toBe(HONOR_CONTAINER);
  });
});
