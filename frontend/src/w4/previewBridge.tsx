"use client";

import { useEffect, useRef } from "react";
import { useShallow } from "zustand/react/shallow";

import { resetHapticHistory } from "../theme/haptics";
import { validatePalettePayload, validateSnapshot } from "../theme/palette";
import { PreviewThemeAdapter } from "../theme/preview-theme-adapter";
import { themeStore } from "../theme/theme-store";
import { useApp } from "./store";
import { callSession } from "./v3/callSession";
import type { PreviewCommand, PreviewEvent, PreviewMessage } from "./previewProtocol";

const CHANNEL = "speakeasy-design-preview";

/**
 * Typed, origin-checked bridge between /design-preview and the real app running
 * in an iframe. Only active when the iframe URL has ?preview=1.
 */
export function PreviewBridge({ variant }: { variant: string }) {
  const readyRef = useRef(false);
  const friends = useApp(useShallow((state) => state.friends));
  const friendsRef = useRef(friends);
  friendsRef.current = friends;

  useEffect(() => {
    if (!readyRef.current) readyRef.current = true;
    const parent = window.parent;
    const sameOrigin = window.location.origin;
    const send = (payload: PreviewEvent) => {
      const message: PreviewMessage<PreviewEvent> = { channel: CHANNEL, payload };
      try {
        parent.postMessage(message, sameOrigin);
      } catch {
        /* parent unavailable (standalone tab) */
      }
    };

    let longTasks: number[] = [];
    let perfObserver: PerformanceObserver | null = null;
    try {
      perfObserver = new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          longTasks.push(entry.duration);
          if (longTasks.length > 30) longTasks.shift();
        }
      });
      perfObserver.observe({ type: "longtask", buffered: true });
    } catch {
      perfObserver = null;
    }

    let frames = 0;
    let fpsStarted = performance.now();
    let fps = 0;
    let raf = 0;
    const tick = () => {
      frames += 1;
      const now = performance.now();
      if (now - fpsStarted > 1200) {
        fps = Math.round((frames * 1000) / (now - fpsStarted));
        frames = 0;
        fpsStarted = now;
      }
      raf = window.requestAnimationFrame(tick);
    };
    raf = window.requestAnimationFrame(tick);

    const metrics = () => {
      const width = window.innerWidth;
      const height = window.innerHeight;
      const overflow: Array<{ tag: string; className: string; right: number; left: number }> = [];
      const nodes = document.querySelectorAll("*").length;
      let images = 0;
      const documentOverflows = document.documentElement.scrollWidth > Math.ceil(width) + 1;
      for (const el of Array.from(document.querySelectorAll("*"))) {
        if (el instanceof HTMLImageElement) images += 1;
        if (!documentOverflows || overflow.length >= 20) continue;
        const rect = el.getBoundingClientRect();
        if (rect.width > 0 && (rect.right > width + 1.5 || rect.left < -1.5)) {
          overflow.push({
            tag: el.tagName.toLowerCase(),
            className: typeof el.className === "string" ? el.className.slice(0, 120) : "",
            right: Math.round(rect.right),
            left: Math.round(rect.left),
          });
        }
      }
      send({
        type: "metrics",
        viewport: { width, height },
        scroll: { width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight },
        overflow,
        longTasks: longTasks.slice(),
        fps,
        nodes,
        images,
        theme: {
          label: themeStore.getSnapshot().snapshot.label,
          mode: themeStore.getSnapshot().mode,
          fingerprint: themeStore.getSnapshot().snapshot.fingerprint,
        },
      });
    };

    const sendTheme = () => {
      const state = themeStore.getSnapshot();
      send({ type: "theme", theme: state.snapshot, mode: state.mode });
    };

    const onMessage = (event: MessageEvent) => {
      if (event.origin !== sameOrigin || event.source !== parent) return;
      const data = event.data as PreviewMessage<PreviewCommand> | undefined;
      if (!data || data.channel !== CHANNEL || !data.payload) return;
      const command = data.payload;
      switch (command.type) {
        case "theme-mode":
          themeStore.setPreference(command.mode);
          break;
        case "theme-honor":
          themeStore.resetToHonor();
          break;
        case "theme-snapshot": {
          const checked = validateSnapshot(command.snapshot);
          if (checked.ok && checked.snapshot) themeStore.applySnapshot(checked.snapshot, { persist: false });
          break;
        }
        case "theme-palette": {
          const checked = validatePalettePayload(command.payload);
          if (checked.ok && checked.payload) PreviewThemeAdapter.fromPhonePalette(checked.payload);
          break;
        }
        case "settings":
          if (typeof command.transparency === "boolean") themeStore.setTransparency(command.transparency);
          if (typeof command.reducedMotion === "boolean") themeStore.setReducedMotion(command.reducedMotion);
          if (command.fontScale) {
            document.documentElement.dataset.fontScale = String(command.fontScale);
            document.documentElement.style.setProperty("--font-scale", String(command.fontScale));
          }
          break;
        case "environment":
          if (typeof command.safeTop === "number") {
            document.documentElement.style.setProperty("--safe-top", `${command.safeTop}px`);
          }
          if (typeof command.safeBottom === "number") {
            document.documentElement.style.setProperty("--safe-bottom", `${command.safeBottom}px`);
          }
          if (typeof command.ime === "number") {
            document.documentElement.style.setProperty("--ime-height", `${command.ime}px`);
          }
          break;
        case "navigate": {
          if (/^#\/(chats|chat\/[\w-]+|discover|discoverFeed|discoverPeople|persona\/[\w-]+|events|contacts|me|expressions|settings)$/.test(command.route)) {
            window.location.hash = command.route;
          }
          break;
        }
        case "call": {
          if (command.action === "end") {
            void callSession.end();
            break;
          }
          if (command.action === "minimize") {
            callSession.minimize();
            break;
          }
          if (command.action === "expand") {
            callSession.expand();
            break;
          }
          const friendId = command.friendId || "fixture-1";
          const friend =
            friendsRef.current.find((f) => f.id === friendId) ?? {
              id: friendId,
              name: "Callum",
            };
          callSession.startPreview(friend, "speaking");
          break;
        }
        case "request-metrics":
          metrics();
          break;
        case "request-state":
          sendTheme();
          metrics();
          break;
      }
    };

    const onHaptic = (event: Event) => {
      const detail = (event as CustomEvent<{ type: string; performed: boolean; platform: string; at: number }>).detail;
      send({ type: "haptic", kind: detail.type, performed: detail.performed, platform: detail.platform, at: detail.at });
    };

    const unsubscribeTheme = themeStore.subscribe(sendTheme);
    window.addEventListener("message", onMessage);
    window.addEventListener("speakeasy:haptic", onHaptic);
    window.addEventListener("speakeasy:preview-ready", metrics);

    const initialTimer = window.setTimeout(() => {
      const state = themeStore.getSnapshot();
      send({
        type: "ready",
        variant,
        theme: state.snapshot,
        mode: state.mode,
        settings: {
          ...state.settings,
          fontScale: document.documentElement.dataset.fontScale ?? "1",
        },
      });
      metrics();
      if (variant === "call") {
        window.setTimeout(() => {
          const friend = friendsRef.current[0] ?? { id: "fixture-1", name: "Callum" };
          callSession.startPreview(friend, "speaking");
        }, 260);
      }
    }, 120);

    const metricsTimer = window.setInterval(metrics, 2500);

    return () => {
      window.clearTimeout(initialTimer);
      window.clearInterval(metricsTimer);
      window.cancelAnimationFrame(raf);
      perfObserver?.disconnect();
      unsubscribeTheme();
      window.removeEventListener("message", onMessage);
      window.removeEventListener("speakeasy:haptic", onHaptic);
      window.removeEventListener("speakeasy:preview-ready", metrics);
      resetHapticHistory();
    };
  }, [variant]);

  return null;
}
