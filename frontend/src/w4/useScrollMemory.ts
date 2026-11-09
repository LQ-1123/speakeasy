import { useLayoutEffect, useRef } from "react";

const positions = new Map<string, number>();
export function savedScrollPosition(key: string): number | undefined { return positions.get(key); }
export function useScrollMemory<T extends HTMLElement>(key: string) {
  const ref = useRef<T>(null);
  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return;
    const saved = positions.get(key) ?? 0;
    let restoring = saved > 0;
    const restore = () => {
      if (!restoring) return;
      if (node.scrollHeight - node.clientHeight >= saved) {
        node.scrollTop = saved;
        restoring = false;
        observer.disconnect();
      }
    };
    const observer = new MutationObserver(restore);
    if (restoring) observer.observe(node, { childList: true, subtree: true, attributes: true });
    const frame = requestAnimationFrame(restore);
    const save = () => { if (!restoring) positions.set(key, node.scrollTop); };
    node.addEventListener("scroll", save, { passive: true });
    return () => { save(); observer.disconnect(); cancelAnimationFrame(frame); node.removeEventListener("scroll", save); };
  }, [key]);
  return ref;
}
