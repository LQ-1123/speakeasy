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

/* ------------------------------------------------------------ chat 贴底判定 */

/** 距离底部多少像素内仍算「贴着底部」：比一屏一两个气泡更小，避免误判。 */
export const BOTTOM_SNAP_THRESHOLD = 96;

export interface ScrollMetrics {
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
}

/** 当前滚动位置离底部还有多少像素（夹紧到 0）。 */
export function distanceFromBottom(metrics: ScrollMetrics): number {
  const { scrollTop, scrollHeight, clientHeight } = metrics;
  if (!Number.isFinite(scrollTop) || !Number.isFinite(scrollHeight) || !Number.isFinite(clientHeight)) {
    return Number.POSITIVE_INFINITY;
  }
  return Math.max(0, scrollHeight - scrollTop - clientHeight);
}

/** 是否贴近底部：只有这种情况才允许「新消息自动滚到底」。 */
export function isNearBottom(metrics: ScrollMetrics, threshold = BOTTOM_SNAP_THRESHOLD): boolean {
  return distanceFromBottom(metrics) <= Math.max(0, threshold);
}

/**
 * 滚动后的贴底状态：
 * - forceBottom（自己发消息、进入会话、聚焦输入框）→ 立刻贴底；
 * - 否则沿用上一态，只有仍在底部附近才继续贴底（用户上翻后不打扰）。
 */
export function nextStickState(
  previous: boolean,
  metrics: ScrollMetrics,
  options: { forceBottom?: boolean; threshold?: number } = {},
): boolean {
  if (options.forceBottom) return true;
  return previous && isNearBottom(metrics, options.threshold ?? BOTTOM_SNAP_THRESHOLD);
}

/** 用户主动上翻（scrollTop 明显变小）就退出贴底，即使还在底部阈值内。 */
export function isScrollingUp(previousTop: number, nextTop: number, tolerance = 12): boolean {
  return nextTop < previousTop - Math.max(0, tolerance);
}
