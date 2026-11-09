import type { ThemePalettePayload, ThemePreference, ThemeSnapshot } from "../theme/types";

export type PreviewCommand =
  | { type: "theme-mode"; mode: ThemePreference }
  | { type: "theme-honor" }
  | { type: "theme-snapshot"; snapshot: ThemeSnapshot }
  | { type: "theme-palette"; payload: ThemePalettePayload }
  | { type: "settings"; transparency?: boolean; reducedMotion?: boolean; fontScale?: number }
  | { type: "environment"; safeTop?: number; safeBottom?: number; ime?: number }
  | { type: "call"; action: "start" | "minimize" | "expand" | "end"; friendId?: string }
  | { type: "navigate"; route: string }
  | { type: "request-metrics" }
  | { type: "request-state" };

export type PreviewEvent =
  | { type: "ready"; variant: string; theme: ThemeSnapshot; mode: "light" | "dark"; settings: Record<string, unknown> }
  | { type: "theme"; theme: ThemeSnapshot; mode: "light" | "dark" }
  | { type: "haptic"; kind: string; performed: boolean; platform: string; at: number }
  | {
      type: "metrics";
      viewport: { width: number; height: number };
      scroll: { width: number; height: number };
      overflow: Array<{ tag: string; className: string; right: number; left: number }>;
      longTasks: number[];
      fps: number;
      nodes: number;
      images: number;
      theme: { label: string; mode: string; fingerprint: string };
    };

export interface PreviewMessage<T> {
  channel: "speakeasy-design-preview";
  payload: T;
}

export function isPreviewCommand(input: unknown): input is PreviewCommand {
  if (!input || typeof input !== "object") return false;
  const type = (input as { type?: unknown }).type;
  return typeof type === "string" && ["theme-mode", "theme-honor", "theme-snapshot", "theme-palette", "settings", "environment", "call", "navigate", "request-metrics", "request-state"].includes(type);
}
