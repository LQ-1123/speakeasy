import { create } from "zustand";
import { themeStore } from "../../theme/theme-store";
import { clientApi, nativeBridge, type ClientPreferences } from "./clientApi";

const defaults: ClientPreferences = { messages: true, interactions: true, sound: true, theme: "system" };
interface PreferenceStore {
  value: ClientPreferences;
  error: string;
  load: () => Promise<void>;
  save: (patch: Partial<ClientPreferences>) => Promise<void>;
}

export const usePreferences = create<PreferenceStore>((set, get) => ({
  value: defaults,
  error: "",
  load: async () => {
    try {
      const value = await clientApi.preferences();
      set({ value, error: "" });
      themeStore.setPreference(value.theme);
      nativeBridge()?.setPreferences?.(JSON.stringify(value));
      window.dispatchEvent(new Event("speakeasy:theme-preference"));
    } catch { /* Keep system defaults while offline. */ }
  },
  save: async (patch) => {
    const previous = get().value;
    const next = { ...previous, ...patch };
    set({ value: next, error: "" });
    window.dispatchEvent(new Event("speakeasy:theme-preference"));
    try {
      const value = await clientApi.savePreferences(patch);
      set({ value: { ...next, ...value } });
      nativeBridge()?.setPreferences?.(JSON.stringify({ ...next, ...value }));
      window.dispatchEvent(new Event("speakeasy:theme-preference"));
    } catch {
      set({ value: previous, error: "设置未保存，请检查连接后重试。" });
      window.dispatchEvent(new Event("speakeasy:theme-preference"));
    }
  },
}));
