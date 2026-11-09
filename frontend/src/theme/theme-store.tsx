"use client";

import { useSyncExternalStore } from "react";

import {
  colorsFor,
  fingerprintPalettes,
  honorSnapshot,
  palettePayloadToSnapshot,
  validateSnapshot,
} from "./palette";
import { installSystemThemeListener, readSystemThemeSnapshot } from "./system-theme-adapter";
import type {
  ThemeColors,
  ThemeMode,
  ThemePreference,
  ThemeSettings,
  ThemeSnapshot,
  ThemeState,
} from "./types";

const STORAGE_KEY = "se.theme.v2";
const SETTINGS_KEY = "se.theme.settings.v2";

interface StoredTheme {
  snapshot: ThemeSnapshot;
  preference: ThemePreference;
}

function readStoredTheme(): StoredTheme {
  const fallback = { snapshot: honorSnapshot(1), preference: "system" as ThemePreference };
  if (typeof window === "undefined") return fallback;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw) as { snapshot?: unknown; preference?: string };
    const checked = validateSnapshot(parsed.snapshot);
    if (!checked.ok || !checked.snapshot) return fallback;
    const preference: ThemePreference =
      parsed.preference === "light" || parsed.preference === "dark" ? parsed.preference : "system";
    return { snapshot: checked.snapshot, preference };
  } catch {
    return fallback;
  }
}

function readStoredSettings(): ThemeSettings {
  const fallback: ThemeSettings = {
    preference: "system",
    transparency: true,
    haptics: true,
    reducedMotion: false,
  };
  if (typeof window === "undefined") return fallback;
  try {
    const raw = window.localStorage.getItem(SETTINGS_KEY);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw) as Partial<ThemeSettings>;
    return {
      preference:
        parsed.preference === "light" || parsed.preference === "dark"
          ? parsed.preference
          : "system",
      transparency: parsed.transparency !== false,
      haptics: parsed.haptics !== false,
      reducedMotion: parsed.reducedMotion === true,
    };
  } catch {
    return fallback;
  }
}

function prefersDark(): boolean {
  if (typeof window === "undefined") return false;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
}

function toKebab(role: string): string {
  return role.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`);
}

const ROLE_VARS = [
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

export function applyThemeToRoot(
  colors: ThemeColors,
  mode: ThemeMode,
  settings: Pick<ThemeSettings, "transparency" | "reducedMotion"> & {
    fontScale?: number;
  },
): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  for (const role of ROLE_VARS) {
    root.style.setProperty(`--md-${toKebab(role)}`, colors[role]);
  }
  root.dataset.theme = mode;
  root.style.colorScheme = mode;
  root.dataset.transparency = settings.transparency ? "on" : "off";
  root.dataset.reducedMotion = settings.reducedMotion ? "true" : "false";
  if (settings.fontScale) {
    root.dataset.fontScale = String(settings.fontScale);
    root.style.setProperty("--font-scale", String(settings.fontScale));
  }

  // Keep the browser / Android system chrome in step with the surface colour.
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", colors.surfaceContainerLow);
}

function effectiveMode(preference: ThemePreference): ThemeMode {
  if (preference === "light" || preference === "dark") return preference;
  return prefersDark() ? "dark" : "light";
}

class ThemeStore {
  private state: ThemeState;
  private listeners = new Set<() => void>();
  private media: MediaQueryList | null = null;
  private installed = false;
  private frame = 0;
  private lastAppliedFingerprint = "";
  private hasApplied = false;
  private transitionTimer = 0;
  private systemUnsubscribe: (() => void) | null = null;
  private revision = 1;

  constructor() {
    const stored = readStoredTheme();
    const settings = { ...readStoredSettings(), preference: stored.preference };
    this.state = {
      snapshot: stored.snapshot,
      mode: effectiveMode(settings.preference),
      settings,
    };
  }

  getSnapshot = (): ThemeState => this.state;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private emit(): void {
    for (const listener of this.listeners) listener();
  }

  private commit(next: Partial<ThemeState>, options?: { persist?: boolean; apply?: boolean }): void {
    const preference = next.settings?.preference ?? this.state.settings.preference;
    const mode = effectiveMode(preference);
    this.state = {
      ...this.state,
      ...next,
      mode,
      settings: next.settings ?? this.state.settings,
    };
    if (options?.persist !== false) this.persist();
    if (options?.apply !== false) this.apply();
    this.emit();
  }

  private persist(): void {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({ snapshot: this.state.snapshot, preference: this.state.settings.preference }),
      );
      window.localStorage.setItem(SETTINGS_KEY, JSON.stringify(this.state.settings));
    } catch {
      /* Private mode: keep the in-memory theme. */
    }
  }

  apply(): void {
    if (typeof document === "undefined") return;
    const colors = colorsFor(this.state.snapshot, this.state.mode);
    const fingerprint = `${this.state.snapshot.fingerprint}:${this.state.mode}:${this.state.settings.transparency}:${this.state.settings.reducedMotion}`;
    if (fingerprint === this.lastAppliedFingerprint) return;
    this.lastAppliedFingerprint = fingerprint;
    applyThemeToRoot(colors, this.state.mode, this.state.settings);
    if (this.hasApplied && !this.state.settings.reducedMotion) {
      window.clearTimeout(this.transitionTimer);
      document.documentElement.dataset.themeTransition = "true";
      this.transitionTimer = window.setTimeout(() => {
        delete document.documentElement.dataset.themeTransition;
      }, 240);
    }
    this.hasApplied = true;
  }

  install(): () => void {
    if (this.installed) return () => undefined;
    this.installed = true;
    this.apply();

    if (typeof window !== "undefined") {
      this.media = window.matchMedia("(prefers-color-scheme: dark)");
      const onScheme = () => {
        if (this.state.settings.preference === "system") this.commit({}, { persist: false });
      };
      this.media.addEventListener("change", onScheme);

      this.systemUnsubscribe = installSystemThemeListener(() => this.refreshFromNative());
    }

    this.refreshFromNative();
    return () => this.teardown();
  }

  private teardown(): void {
    if (!this.installed) return;
    this.installed = false;
    this.media = null;
    this.systemUnsubscribe?.();
    this.systemUnsubscribe = null;
  }

  /**
   * Re-sample the system after resume / wallpaper change. The native bridge is
   * optional; web falls back to the deterministic in-app theme.
   */
  refreshFromNative(): void {
    const read = readSystemThemeSnapshot();
    if (!read.snapshot) return;
    const fingerprint = fingerprintPalettes(read.snapshot.palettes);
    if (fingerprint === this.state.snapshot.fingerprint) return;
    this.revision += 1;
    this.commit({ snapshot: { ...read.snapshot, revision: this.revision } }, { persist: true });
  }

  setPreference(preference: ThemePreference): void {
    this.commit({ settings: { ...this.state.settings, preference } });
    if (typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("speakeasy:theme-preference", { detail: preference }));
    }
  }

  setTransparency(transparency: boolean): void {
    this.lastAppliedFingerprint = "";
    this.commit({ settings: { ...this.state.settings, transparency } });
  }

  setHaptics(haptics: boolean): void {
    this.commit({ settings: { ...this.state.settings, haptics } });
  }

  setReducedMotion(reducedMotion: boolean): void {
    this.lastAppliedFingerprint = "";
    this.commit({ settings: { ...this.state.settings, reducedMotion } });
  }

  /** Preview / imported palette. Never meant for the production user flow. */
  applySnapshot(snapshot: ThemeSnapshot, options?: { persist?: boolean }): void {
    this.revision = Math.max(this.revision + 1, snapshot.revision);
    this.lastAppliedFingerprint = "";
    this.commit({ snapshot: { ...snapshot, revision: this.revision } }, { persist: options?.persist === true });
  }

  applyPalettePayload(payload: Parameters<typeof palettePayloadToSnapshot>[0], label?: string): ThemeSnapshot {
    const snapshot = palettePayloadToSnapshot(
      label ? { ...payload, label } : payload,
      this.revision + 1,
    );
    this.applySnapshot(snapshot);
    return snapshot;
  }

  resetToHonor(): ThemeSnapshot {
    const snapshot = honorSnapshot(this.revision + 1);
    this.applySnapshot(snapshot);
    return snapshot;
  }

  currentColors(): ThemeColors {
    return colorsFor(this.state.snapshot, this.state.mode);
  }
}

export const themeStore = new ThemeStore();

export function useTheme(): ThemeState {
  return useSyncExternalStore(themeStore.subscribe, themeStore.getSnapshot, themeStore.getSnapshot);
}

export function themeColors(state?: ThemeState): ThemeColors {
  const value = state ?? themeStore.getSnapshot();
  return colorsFor(value.snapshot, value.mode);
}
