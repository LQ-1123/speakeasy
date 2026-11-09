"use client";

import { useEffect, type ReactNode } from "react";

import { themeStore } from "./theme-store";

/**
 * Installs the one semantic theme pipeline for the app tree. The pre-hydration
 * script already applied the cached palette; this effect syncs the system source
 * and starts listening for foreground / configuration changes.
 */
export function ThemeProvider({ children }: { children: ReactNode }) {
  useEffect(() => themeStore.install(), []);
  return <>{children}</>;
}
