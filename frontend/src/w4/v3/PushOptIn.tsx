"use client";

import { useEffect, useState } from "react";
import { BellRing } from "lucide-react";

import { haptic } from "../../theme/haptics";
import { nativeBridge } from "./clientApi";
import { usePreferences } from "./preferences";
import "../styles/notifications.css";

const PROMPTED_KEY = "speakeasy.push.prompted.v1";

/**
 * 系统通知引导：一张紧凑的底部卡片。
 * 手机端浮在底部导航之上，桌面端待在右下角。
 */
export function PushOptIn() {
  const [visible, setVisible] = useState(false);
  const preferences = usePreferences((state) => state.value);

  useEffect(() => {
    if (!preferences.messages && !preferences.interactions) return;
    try {
      const capabilities = JSON.parse(nativeBridge()?.getCapabilities?.() || "{}");
      if (capabilities.push && !window.localStorage.getItem(PROMPTED_KEY)) setVisible(true);
    } catch {
      /* Browser and unconfigured push have no prompt. */
    }
  }, [preferences.messages, preferences.interactions]);

  if (!visible) return null;

  const dismiss = () => {
    try {
      window.localStorage.setItem(PROMPTED_KEY, "1");
    } catch {
      /* privacy mode */
    }
    setVisible(false);
  };

  return (
    <aside className="se-push-opt-in" aria-label="系统通知">
      <div className="se-push-head">
        <span className="se-push-icon" aria-hidden>
          <BellRing size={20} strokeWidth={1.8} />
        </span>
        <div className="se-push-copy">
          <strong>接收新消息提醒</strong>
          <p>离开应用后，也能知道语伴什么时候回复你。</p>
        </div>
      </div>
      <div className="se-push-actions">
        <button
          onClick={() => {
            haptic("selection");
            dismiss();
          }}
        >
          以后再说
        </button>
        <button
          className="is-primary"
          onClick={() => {
            haptic("save");
            dismiss();
            nativeBridge()?.requestPush?.();
          }}
        >
          开启通知
        </button>
      </div>
    </aside>
  );
}
