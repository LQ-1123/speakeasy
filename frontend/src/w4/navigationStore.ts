import { create } from "zustand";
import type { Route } from "./model";
import { parseRoute } from "./navigation";

interface NavigationStore { route: Route; setRoute: (route: Route) => void }
export const useNavigation = create<NavigationStore>((set) => ({
  route: parseRoute(typeof window === "undefined" ? "" : window.location.hash),
  setRoute: (route) => set({ route }),
}));

const overlays: Array<() => void> = [];
export function registerOverlay(close: () => void): () => void {
  overlays.push(close);
  return () => { const index = overlays.lastIndexOf(close); if (index >= 0) overlays.splice(index, 1); };
}
export function closeTopOverlay(): boolean {
  const close = overlays[overlays.length - 1];
  if (!close) return false;
  close();
  return true;
}
