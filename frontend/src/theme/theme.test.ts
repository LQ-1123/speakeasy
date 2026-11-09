import { describe, expect, it } from "vitest";

import { HONOR_PALETTES } from "./honor-fixture";
import {
  compositeOver,
  contrastChecks,
  contrastRatio,
  fingerprintPalettes,
  honorSnapshot,
  normalizeHex,
  palettePayloadToSnapshot,
  validatePalettePayload,
  validateSnapshot,
} from "./palette";

describe("semantic theme", () => {
  it("normalizes hex values and rejects unsafe colours", () => {
    expect(normalizeHex("#4c5e8b")).toBe("#4C5E8B");
    expect(normalizeHex("#abc")).toBe("#AABBCC");
    expect(normalizeHex("red")).toBeNull();
    expect(normalizeHex("#12")).toBeNull();
  });

  it("keeps the Honor primary pair readable in both modes", () => {
    for (const mode of ["light", "dark"] as const) {
      const checks = contrastChecks(HONOR_PALETTES[mode]);
      for (const check of checks) {
        expect(check.pass, `${mode} ${check.role} = ${check.ratio.toFixed(2)}:1`).toBe(true);
      }
    }
  });

  it("flattens a glass surface before measuring contrast", () => {
    const flattened = compositeOver("#FFFFFF", "#EEEDF4", 0.9);
    const ratio = contrastRatio("#1A1B21", flattened);
    expect(ratio).toBeGreaterThan(4.5);
  });

  it("validates complete snapshots and rejects missing roles", () => {
    const snapshot = honorSnapshot(3);
    expect(validateSnapshot(snapshot).ok).toBe(true);
    const broken = {
      ...snapshot,
      palettes: { light: { ...snapshot.palettes.light, primary: "not-a-colour" }, dark: snapshot.palettes.dark },
    };
    expect(validateSnapshot(broken).ok).toBe(false);
  });

  it("keeps a stable fingerprint for identical palettes and changes it on edit", () => {
    const base = fingerprintPalettes(HONOR_PALETTES);
    expect(fingerprintPalettes(HONOR_PALETTES)).toBe(base);
    expect(
      fingerprintPalettes({
        light: { ...HONOR_PALETTES.light, primary: "#000000" },
        dark: HONOR_PALETTES.dark,
      }),
    ).not.toBe(base);
  });

  it("merges a partial phone palette over the deterministic fallback", () => {
    const next = palettePayloadToSnapshot(
      {
        label: "ADB import",
        source: "honor-500-pro-home-wallpaper",
        palettes: {
          light: { primary: "#4C5E8B", primaryContainer: "#B9CBFF" },
          dark: { primary: "#B8C6EE" },
        },
      },
      9,
    );
    expect(next.palettes.light.primary).toBe("#4C5E8B");
    expect(next.palettes.light.onSurface).toBe(HONOR_PALETTES.light.onSurface);
    expect(next.revision).toBe(9);
    expect(next.label).toBe("ADB import");
  });

  it("rejects unknown palette roles", () => {
    expect(validatePalettePayload({ palettes: { light: { nope: "#fff" }, dark: {} } }).ok).toBe(false);
  });
});
