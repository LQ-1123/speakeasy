"use client";

/**
 * 推荐流里的商业合作卡片（`kind="sponsored"`）。
 *
 * - 呈现「广告」标识、品牌、审核文案与落地页；关闭/举报后本页隐藏并上报事件；
 * - **可见曝光**：进入视口并连续可见 ≥1 秒才上报 `impression`（同投放只报一次）；
 * - 落地页点击上报 `click` 后在新窗口打开（只允许 http/https）；
 * - 未成年人/未开启账号不会拿到这类条目（服务端判定）。
 */

import { useEffect, useRef, useState } from "react";

import { adEvent } from "./accountClient";
import {
  adEventBody,
  hideDelivery,
  impressionAlreadyReported,
  markImpressionReported,
  safeLandingUrl,
  shouldReportImpression,
} from "./adEvents";

export interface SponsoredItem {
  kind: "sponsored";
  id: string;
  delivery_id?: string;
  campaign_id?: string;
  brand_name?: string;
  title?: string;
  content?: string;
  label?: string;
  landing_url?: string;
}

function deliveryOf(item: SponsoredItem): string {
  return item.delivery_id || item.id || "";
}

export function SponsoredCard({
  item,
  onHide,
}: {
  item: SponsoredItem;
  onHide?: (deliveryId: string) => void;
}) {
  const deliveryId = deliveryOf(item);
  const ref = useRef<HTMLDivElement | null>(null);
  const seenAt = useRef<number>(0);
  const [hidden, setHidden] = useState(false);
  const [feedback, setFeedback] = useState("");

  useEffect(() => {
    if (!deliveryId || impressionAlreadyReported(deliveryId)) return;
    const node = ref.current;
    if (!node || typeof IntersectionObserver === "undefined") return;
    seenAt.current = 0;
    const observer = new IntersectionObserver(
      (entries) => {
        const entry = entries[0];
        if (!entry) return;
        if (entry.isIntersecting) {
          seenAt.current = Date.now();
          return;
        }
        if (!seenAt.current) return;
        const visibleMs = Date.now() - seenAt.current;
        seenAt.current = 0;
        if (!shouldReportImpression(visibleMs)) return;
        markImpressionReported(deliveryId);
        void adEvent(deliveryId, "impression", adEventBody("impression", visibleMs).payload as Record<string, unknown>).catch(() => undefined);
      },
      { threshold: 0.6 },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [deliveryId]);

  if (hidden) return null;
  const landing = safeLandingUrl(item.landing_url);

  function dismiss(type: "close" | "report") {
    setHidden(true);
    hideDelivery(deliveryId);
    setFeedback(type === "close" ? "已关闭这条推荐" : "已举报，运营会核查");
    if (deliveryId) void adEvent(deliveryId, type).catch(() => undefined);
    onHide?.(deliveryId);
  }

  return (
    <div
      ref={ref}
      className="disc-card"
      data-sponsored="1"
      style={{
        border: "1px solid rgba(120,120,120,.22)",
        background: "linear-gradient(180deg, rgba(79,124,255,.07), rgba(79,124,255,.02))",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, opacity: 0.8 }}>
        <span
          style={{
            padding: "1px 8px",
            borderRadius: 999,
            background: "rgba(255,166,0,.16)",
            color: "#a96a00",
            fontWeight: 600,
          }}
        >
          {item.label || "广告"}
        </span>
        <span>{item.brand_name || "合作品牌"}</span>
      </div>
      {item.title ? <div style={{ fontWeight: 600, fontSize: 15 }}>{item.title}</div> : null}
      <p style={{ margin: 0, fontSize: 14, lineHeight: 1.55, whiteSpace: "pre-wrap" }}>
        {item.content || ""}
      </p>
      <div style={{ display: "flex", gap: 10, alignItems: "center", fontSize: 12 }}>
        {landing ? (
          <a
            href={landing}
            target="_blank"
            rel="noopener noreferrer"
            onClick={() => {
              if (deliveryId) void adEvent(deliveryId, "click").catch(() => undefined);
            }}
            style={{ color: "var(--accent, #4f7cff)", fontWeight: 600 }}
          >
            了解详情
          </a>
        ) : null}
        <button type="button" onClick={() => dismiss("close")} style={{ background: "none", border: "none", cursor: "pointer", opacity: 0.7 }}>
          不感兴趣
        </button>
        <button type="button" onClick={() => dismiss("report")} style={{ background: "none", border: "none", cursor: "pointer", opacity: 0.7 }}>
          举报
        </button>
        {feedback ? <span style={{ opacity: 0.7 }}>{feedback}</span> : null}
      </div>
    </div>
  );
}
