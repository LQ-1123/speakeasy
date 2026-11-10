"use client";

import { useEffect, useState } from "react";
import { BellRing } from "lucide-react";

import { haptic } from "../../theme/haptics";
import { nativeBridge, type NativeNotificationStatus } from "./clientApi";
import { usePreferences } from "./preferences";
import {
  markPushPrompted,
  onPushPrompted,
  openSystemNotificationSettings,
  PUSH_PROMPT_KEY,
  requestSystemNotificationPermission,
  systemNotificationStatus,
} from "../notify";
import "../styles/notifications.css";

/**
 * 系统通知引导：一张紧凑的底部卡片。
 * 手机端浮在底部导航之上，桌面端待在右下角。
 *
 * 卡片内容来自原生真实状态：未配置 JPush 的安装包会明确说明「未配置推送服务」，
 * 权限被拒时直接引导到系统设置，而不是给一个点了没反应的按钮。
 */
export function PushOptIn() {
  const [visible, setVisible] = useState(false);
  const [status, setStatus] = useState<NativeNotificationStatus | null>(null);
  const preferences = usePreferences((state) => state.value);

  useEffect(() => {
    const native = nativeBridge();
    if (!native?.getCapabilities) return;
    let capabilities: { nativeCalls?: boolean; localNotifications?: boolean; push?: boolean } = {};
    try {
      capabilities = JSON.parse(native.getCapabilities() || "{}");
    } catch {
      return;
    }
    /* 普通浏览器没有本地系统通知能力，不显示这张卡。 */
    if (!capabilities.localNotifications) return;
    const current = systemNotificationStatus();
    setStatus(current);
    if (!preferences.messages && !preferences.interactions) return;
    try {
      if (window.localStorage.getItem(PUSH_PROMPT_KEY)) return;
    } catch {
      /* privacy mode */
    }
    const needsAttention = !current || current.permission !== "granted" || !current.enabled || !current.push;
    if (needsAttention) setVisible(true);
  }, [preferences.messages, preferences.interactions]);

  /* 设置页里已经处理过时，不再同时弹引导卡。 */
  useEffect(() => onPushPrompted(() => setVisible(false)), []);

  if (!visible) return null;

  const dismiss = () => {
    markPushPrompted();
    setVisible(false);
  };

  const pushUnconfigured = status ? !status.push : false;
  const permissionBlocked = status ? status.permission === "denied" || !status.enabled : false;

  const title = pushUnconfigured ? "系统通知（当前安装包未配置推送）" : "接收新消息提醒";
  const description = pushUnconfigured
    ? "当前安装包未配置推送服务，离开应用后可能收不到提醒；应用内提醒仍然可用。带推送凭据重新构建后才能收到锁屏通知。"
    : permissionBlocked
      ? "通知权限未开启。你仍会在应用内看到提醒，去系统设置允许后才能收到锁屏和后台通知。"
      : "离开应用后，也能知道语伴什么时候回复你。";

  const primary = () => {
    haptic("save");
    if (permissionBlocked) {
      openSystemNotificationSettings();
      dismiss();
      return;
    }
    if (pushUnconfigured) {
      const prompted = requestSystemNotificationPermission();
      if (!prompted) openSystemNotificationSettings();
      dismiss();
      return;
    }
    dismiss();
    nativeBridge()?.requestPush?.();
  };

  return (
    <aside className="se-push-opt-in" aria-label="系统通知">
      <div className="se-push-head">
        <span className="se-push-icon" aria-hidden>
          <BellRing size={20} strokeWidth={1.8} />
        </span>
        <div className="se-push-copy">
          <strong>{title}</strong>
          <p>{description}</p>
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
        <button className="is-primary" onClick={primary}>
          {permissionBlocked ? "去系统设置" : pushUnconfigured ? "允许应用内通知" : "开启通知"}
        </button>
      </div>
    </aside>
  );
}
