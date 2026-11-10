import { afterEach, describe, expect, it } from "vitest";

import { contrastChecks, honorSnapshot } from "./palette";
import { readSystemThemeSnapshot } from "./system-theme-adapter";
import { THEME_ROLES } from "./types";

/** The legacy Android 12+ bridge only exposes accent/onAccent/dark/insets. */
const LEGACY_KEYS = ["getCapabilities", "getTheme"] as const;

function installBridge(bridge: Record<string, unknown>): void {
  (globalThis as unknown as { window: unknown }).window = { SpeakEasyNative: bridge };
}

function installLegacyBridge(accent: string): void {
  installBridge({
    getCapabilities: () => JSON.stringify({ dynamicColor: true }),
    getTheme: () =>
      JSON.stringify({
        accent,
        onAccent: "#FFFFFF",
        dark: false,
        insets: { top: 24, bottom: 24, ime: 0 },
      }),
  });
}

afterEach(() => {
  delete (globalThis as unknown as { window?: unknown }).window;
});

describe("system theme adapter legacy accent bridge", () => {
  it("expands a lone wallpaper accent into the complete M3 palette", () => {
    installLegacyBridge("#8C4A2F");
    const read = readSystemThemeSnapshot();
    expect(read.snapshot).not.toBeNull();
    const snapshot = read.snapshot!;

    expect(snapshot.source).toBe("android-system");
    expect(snapshot.meta?.sourceColor).toBe("#8C4A2F");
    expect(snapshot.meta?.completeness).toBe("partial");
    expect(snapshot.meta?.note).toContain("M3");
    expect(read.complete).toBe(false);

    for (const mode of ["light", "dark"] as const) {
      const colors = snapshot.palettes[mode];
      for (const role of THEME_ROLES) {
        expect(colors[role], `${mode} ${role}`).toMatch(/^#[0-9A-F]{6}$/);
      }
      // The reported regression: containers used to stay Honor light blue.
      expect(colors.primaryContainer).not.toBe("#B9CBFF");
      for (const check of contrastChecks(colors)) {
        expect(check.pass, `${mode} ${check.role} = ${check.ratio.toFixed(2)}:1`).toBe(true);
      }
    }
    // Same generator as the system palette: brown accent → warm brown family.
    expect(snapshot.palettes.light.primary).toBe("#8E4D32");
    expect(snapshot.palettes.light.primaryContainer).toBe("#FFDBCE");
  });

  it("changes the fingerprint when the wallpaper accent changes so ThemeStore re-applies", () => {
    installLegacyBridge("#8C4A2F");
    const brown = readSystemThemeSnapshot().snapshot!;
    installLegacyBridge("#2E7D32");
    const green = readSystemThemeSnapshot().snapshot!;

    // ThemeStore.refreshFromNative compares fingerprintPalettes() output; a new
    // accent must therefore produce a new fingerprint and a visible re-apply.
    expect(green.fingerprint).not.toBe(brown.fingerprint);
    expect(green.palettes.light.primary).not.toBe(brown.palettes.light.primary);
    expect(green.palettes.light.primaryContainer).not.toBe(brown.palettes.light.primaryContainer);
    expect(green.palettes.dark.primaryContainer).not.toBe(brown.palettes.dark.primaryContainer);
  });

  it("prefers a complete getThemeSnapshot() over the legacy accent path", () => {
    const complete = honorSnapshot(7);
    installBridge({
      getCapabilities: () => JSON.stringify({ dynamicColor: true, semanticColors: true }),
      getThemeSnapshot: () => JSON.stringify(complete),
      getTheme: () => JSON.stringify({ accent: "#8C4A2F" }),
    });

    const read = readSystemThemeSnapshot();
    expect(read.complete).toBe(true);
    expect(read.snapshot?.source).toBe("honor-500-pro-home-wallpaper");
    expect(read.snapshot?.revision).toBe(7);
    expect(read.snapshot?.palettes).toEqual(complete.palettes);
  });

  it("returns no snapshot when the native accent is missing, so ThemeStore keeps the stored theme", () => {
    installBridge({
      getCapabilities: () => JSON.stringify({ dynamicColor: true }),
      getTheme: () => JSON.stringify({ dark: false, insets: { top: 24, bottom: 24, ime: 0 } }),
    });

    const read = readSystemThemeSnapshot();
    // A transiently unreadable bridge must NOT hand back the Honor light-blue
    // fallback as an "android-system" snapshot: refreshFromNative would then
    // overwrite a stored warm wallpaper palette with the old blue fixture.
    expect(read.snapshot).toBeNull();
    expect(read.complete).toBe(false);
  });

  it("returns no snapshot for an empty legacy payload", () => {
    installBridge({
      getCapabilities: () => JSON.stringify({ dynamicColor: true }),
      getTheme: () => "{}",
    });
    expect(readSystemThemeSnapshot().snapshot).toBeNull();
  });

  it("returns no snapshot when there is no native bridge at all", () => {
    const read = readSystemThemeSnapshot();
    expect(read.snapshot).toBeNull();
    expect(read.complete).toBe(false);
  });

  it("ignores unusable accent values instead of generating garbage", () => {
    installBridge({
      getCapabilities: () => JSON.stringify({ dynamicColor: true }),
      getTheme: () => JSON.stringify({ accent: "not-a-colour" }),
    });
    expect(readSystemThemeSnapshot().snapshot).toBeNull();
  });

  it("documents the legacy bridge surface it consumes", () => {
    // Guards against silently depending on fields the Android bridge never sends.
    expect(LEGACY_KEYS).toEqual(["getCapabilities", "getTheme"]);
  });
});
