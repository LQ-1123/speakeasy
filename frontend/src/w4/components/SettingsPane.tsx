"use client";

/**
 * 设置。只保留 提醒 / 外观与触感 / 隐私与数据 / 关于。
 *
 * 模型、密钥、语音识别/合成配置已全部收进本机管理端（/admin），客户端不再提供
 * 任何配置入口，避免零基础客户误改后整个应用不可用。
 *
 * 外观三项（主题 / 半透明 / 触感）直接写进 themeStore，主题与触感会在本机持久化；
 * 主题偏好同时保留原有的后端 preferences 保存调用。
 */

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Bell, ChevronLeft, Info, Palette, ShieldCheck, Sparkles } from "lucide-react";

import { haptic } from "../../theme/haptics";
import { themeStore, useTheme } from "../../theme/theme-store";
import type { ThemePreference } from "../../theme/types";

import type { Route } from "../model";
import { Segmented, Switch } from "../ui";
import { RecommendAdsSection } from "./RecommendAdsSection";
import { usePreferences } from "../v3/preferences";
import { nativeBridge, type ClientPreferences, type NativeNotificationStatus } from "../v3/clientApi";
import {
  markPushPrompted,
  openSystemNotificationSettings,
  requestSystemNotificationPermission,
  systemNotificationStatus,
} from "../notify";
import "../styles/profile.css";
import "../styles/notifications.css";

type SettingsSectionKey = "notify" | "recommend" | "appearance" | "privacy" | "about";

const SECTIONS: ReadonlyArray<{ key: SettingsSectionKey; label: string; icon: ReactNode }> = [
  { key: "notify", label: "提醒", icon: <Bell size={18} strokeWidth={1.8} /> },
  { key: "recommend", label: "推荐", icon: <Sparkles size={18} strokeWidth={1.8} /> },
  { key: "appearance", label: "外观与触感", icon: <Palette size={18} strokeWidth={1.8} /> },
  { key: "privacy", label: "隐私与数据", icon: <ShieldCheck size={18} strokeWidth={1.8} /> },
  { key: "about", label: "关于", icon: <Info size={18} strokeWidth={1.8} /> },
];

const THEME_OPTIONS: Array<{ value: ThemePreference; label: string }> = [
  { value: "system", label: "跟随系统" },
  { value: "light", label: "浅色" },
  { value: "dark", label: "深色" },
];

export function SettingsPane({
  section,
  navigate,
}: {
  section?: string;
  navigate: (r: Route) => void;
}) {
  const initialSection: SettingsSectionKey = SECTIONS.some((item) => item.key === section)
    ? (section as SettingsSectionKey)
    : "notify";
  const [active, setActive] = useState<SettingsSectionKey>(initialSection);

  return (
    <div className="w4-main">
      <div className="w4-head">
        <button className="w4-icon-btn" onClick={() => navigate({ name: "me" })} aria-label="返回">
          <ChevronLeft size={18} />
        </button>
        <div className="w4-head-main">
          <div className="w4-head-title">设置</div>
        </div>
      </div>

      <div className="w4-settings-layout">
        <nav className="w4-settings-nav" aria-label="设置分区">
          {SECTIONS.map((item) => (
            <button
              key={item.key}
              className={`w4-settings-nav-btn ${active === item.key ? "on" : ""}`}
              aria-current={active === item.key ? "page" : undefined}
              onClick={() => {
                if (active === item.key) return;
                haptic("selection");
                setActive(item.key);
              }}
            >
              <span className="w4-settings-nav-icon">{item.icon}</span>
              {item.label}
            </button>
          ))}
        </nav>

        <div className="w4-settings-body">
          <div className="w4-settings-card">
            {active === "notify" ? <NotifySection /> : null}
            {active === "recommend" ? <RecommendAdsSection /> : null}
            {active === "appearance" ? <AppearanceSection /> : null}
            {active === "privacy" ? <PrivacySection /> : null}
            {active === "about" ? <AboutSection /> : null}
          </div>
        </div>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- 提醒 */

function NotifySection() {
  const preferences = usePreferences((state) => state.value);
  const save = usePreferences((state) => state.save);
  const error = usePreferences((state) => state.error);
  const native = typeof window === "undefined" ? undefined : nativeBridge();
  const [status, setStatus] = useState<NativeNotificationStatus | null>(null);

  const refresh = useCallback(() => setStatus(systemNotificationStatus()), []);

  useEffect(() => {
    refresh();
    /* Native push events (registered / error / denied) arrive as window events;
       without this the card keeps showing the status captured on mount. */
    const onNative = (event: Event) => {
      const detail = (event as CustomEvent<{ type?: string }>).detail;
      if (detail?.type === "push") refresh();
    };
    window.addEventListener("speakeasy:native", onNative);
    return () => window.removeEventListener("speakeasy:native", onNative);
  }, [refresh]);

  /* Registration is asynchronous (JPush + network). Poll while waiting so a
     success/error eventually replaces 「正在注册推送」 without a manual refresh. */
  const waiting =
    Boolean(status) &&
    Boolean(status?.push) &&
    status?.pushState !== "registered" &&
    status?.permission === "granted" &&
    Boolean(status?.enabled);
  useEffect(() => {
    if (!waiting) return;
    const timer = window.setInterval(refresh, 2000);
    const stop = window.setTimeout(() => window.clearInterval(timer), 90_000);
    return () => {
      window.clearInterval(timer);
      window.clearTimeout(stop);
    };
  }, [waiting, refresh]);

  /** 开关先同步给原生壳，后端保存失败也不会让系统通知行为与界面不一致。 */
  const syncNative = (patch: Partial<ClientPreferences>) => {
    try {
      native?.setPreferences?.(JSON.stringify({ ...preferences, ...patch }));
    } catch {
      /* older bridge */
    }
  };

  const permissionBlocked = status ? status.permission === "denied" || !status.enabled : false;
  const pushUnconfigured = status ? !status.push : false;
  const pushFailed =
    Boolean(status?.push) &&
    status?.pushState !== "registered" &&
    !permissionBlocked &&
    status?.lastPushStatus === "error";
  const statusText = !status
    ? "当前环境不支持系统通知。"
    : permissionBlocked
      ? "未开启（通知权限或系统通知总开关被关闭）。"
      : pushUnconfigured
        ? "当前安装包未配置推送服务。"
        : status.pushState === "registered"
          ? "已开启，推送已注册。"
          : pushFailed
            ? "已开启，推送注册失败。"
            : "已开启，正在注册推送…";
  const hint = pushUnconfigured
    ? "应用内提醒仍然可用；锁屏与后台提醒需要在构建时配置 JPUSH_APPKEY / JPUSH_MASTER_SECRET 并重新打包。"
    : permissionBlocked
      ? "在系统设置中允许通知后，锁屏和后台提醒才会出现。"
      : pushFailed
        ? `${status?.lastPushMessage || "推送注册失败"}（点「重新注册推送」重试，或检查网络后重试）`
        : null;
  /* 注册超时时最有用的诊断：极光长连接是否建立。未连接通常意味着手机网络
     访问不到 sdk.jpush.cn（切换 Wi-Fi/移动网络或代理后再试）。 */
  const pushDiagnostic =
    native && status && status.push && status.pushState !== "registered"
      ? [
          `通道：${status.jpushConnected ? "已连接" : "未连接"}`,
          `推送开关：${status.pushStopped ? "已停止" : "运行中"}`,
          status.jpushConnected ? "" : "（手机可能无法访问极光服务器）",
        ]
          .filter(Boolean)
          .join(" · ")
      : null;

  return (
    <section>
      <SectionHead title="提醒" desc="只在你允许的范围内推送，随时可以关掉。" />
      {native ? (
        <div className="se-push-status">
          <div className="se-push-status-head">
            <strong>系统通知</strong>
            <span className={permissionBlocked || pushUnconfigured ? "is-warn" : "is-ok"}>{statusText}</span>
          </div>
          {hint ? <p className="se-push-status-hint">{hint}</p> : null}
          {pushDiagnostic ? <p className="se-push-status-hint is-diagnostic">{pushDiagnostic}</p> : null}
          {status?.networkResult ? (
            <p className="se-push-status-hint is-diagnostic" style={{ whiteSpace: "pre-line" }}>
              {status.networkResult}
            </p>
          ) : null}
          <div className="se-push-status-actions">
            {permissionBlocked ? (
              <button
                onClick={() => {
                  haptic("selection");
                  openSystemNotificationSettings();
                  refresh();
                }}
              >
                去系统设置
              </button>
            ) : pushUnconfigured ? (
              <button
                onClick={() => {
                  haptic("selection");
                  if (!requestSystemNotificationPermission()) openSystemNotificationSettings();
                  markPushPrompted();
                  refresh();
                }}
              >
                允许应用内通知
              </button>
            ) : (
              <button
                onClick={() => {
                  haptic("selection");
                  native.requestPush?.();
                  refresh();
                }}
              >
                重新注册推送
              </button>
            )}
            <button
              onClick={() => {
                haptic("selection");
                refresh();
              }}
            >
              刷新状态
            </button>
            <button
              onClick={() => {
                haptic("selection");
                native.probePushNetwork?.();
                window.setTimeout(refresh, 6500);
              }}
            >
              网络检测
            </button>
          </div>
        </div>
      ) : null}
      <SettingRow
        title="好友发来新语音"
        desc="离开页面时，好友的回复会推送到系统通知。"
        control={
          <Switch
            label="好友消息通知"
            on={preferences.messages}
            onChange={(messages) => {
              haptic("toggle");
              syncNative({ messages });
              void save({ messages });
              if (messages) nativeBridge()?.requestPush?.();
            }}
          />
        }
      />
      <SettingRow
        title="互动与好友申请"
        desc="有人点赞、评论，或想成为你的语伴时提醒。"
        control={
          <Switch
            label="互动与好友申请通知"
            on={preferences.interactions}
            onChange={(interactions) => {
              haptic("toggle");
              syncNative({ interactions });
              void save({ interactions });
            }}
          />
        }
      />
      <SettingRow
        title="提示音"
        desc="新消息和新好友申请到达时，播放一声提示音。"
        control={
          <Switch
            label="提示音"
            on={preferences.sound}
            onChange={(sound) => {
              haptic("toggle");
              syncNative({ sound });
              void save({ sound });
            }}
          />
        }
      />
      {error ? (
        <p role="alert" className="w4-settings-error">
          {error}
        </p>
      ) : null}
    </section>
  );
}

/* ---------------------------------------------------------------- 外观 */

function AppearanceSection() {
  const theme = useTheme();
  const save = usePreferences((state) => state.save);
  const error = usePreferences((state) => state.error);
  const preference = theme.settings.preference;

  const chooseTheme = (next: ThemePreference) => {
    if (next === preference) return;
    haptic("selection");
    themeStore.setPreference(next);
    /* 原有后端字段保持不变：主题偏好也同步到账户。 */
    void save({ theme: next });
  };

  return (
    <section>
      <SectionHead
        title="外观与触感"
        desc={`跟随系统，或固定浅色 / 深色（当前：${theme.mode === "dark" ? "深色" : "浅色"}）。`}
      />
      <div className="w4-settings-row is-stacked">
        <div className="w4-settings-row-copy">
          <div className="w4-settings-row-title">主题</div>
          <div className="w4-settings-row-desc">跟随系统，或固定使用浅色、深色。</div>
        </div>
        <div className="w4-settings-seg" role="group" aria-label="主题">
          <Segmented<ThemePreference>
            value={preference}
            options={THEME_OPTIONS}
            onChange={chooseTheme}
          />
        </div>
      </div>
      <SettingRow
        title="半透明材质"
        desc="导航、顶栏和通知会透出背后的颜色。"
        control={
          <Switch
            label="半透明材质"
            on={theme.settings.transparency}
            onChange={(transparency) => {
              haptic("toggle");
              themeStore.setTransparency(transparency);
            }}
          />
        }
      />
      <SettingRow
        title="触感反馈"
        desc="切换开关、保存设置时给出轻微的触感反馈。"
        control={
          <Switch
            label="触感反馈"
            on={theme.settings.haptics}
            onChange={(haptics) => {
              /* 关闭时先给一次反馈，再让开关失效；开启后立即生效。 */
              if (haptics) {
                themeStore.setHaptics(true);
                haptic("toggle");
              } else {
                haptic("toggle");
                themeStore.setHaptics(false);
              }
            }}
          />
        }
      />
      {error ? (
        <p role="alert" className="w4-settings-error">
          {error}
        </p>
      ) : null}
    </section>
  );
}

/* ---------------------------------------------------------------- 隐私 */

function PrivacySection() {
  return (
    <section>
      <SectionHead title="隐私与数据" desc="聊天、表达和个人资料由你的账户保存。" />
      <SettingRow
        title="聊天记录"
        desc="包含语音原文、译文和好友记住的事情。"
      />
      <SettingRow
        title="设备权限"
        desc="麦克风用于语音对话；通知权限可在系统设置中调整。"
      />
      <SettingRow
        title="好友能记住什么"
        desc="只记你主动说过的信息，不收集通讯录、位置和相册。"
      />
    </section>
  );
}

/* ---------------------------------------------------------------- 关于 */

function AboutSection() {
  return (
    <section>
      <SectionHead
        title="关于 Sonder"
        desc="一款用来把「不敢开口」变成「顺口说出来」的工具。"
      />
      <div className="w4-about">
        <div className="w4-about-row">
          <span>版本</span>
          <span>4.1.2</span>
        </div>
        <div className="w4-about-row">
          <span>创作者</span>
          <span>Sonder 团队</span>
        </div>
        <div className="w4-about-row">
          <span>数据</span>
          <span>账户保存</span>
        </div>
        <div className="w4-about-row">
          <span>头像素材</span>
          <span>Personas by Draftbit（CC BY 4.0）· DiceBear 生成</span>
        </div>
      </div>
      <p className="w4-about-text">
        你缺的不是词汇量，是在真实压力下用另一种语言反应的条件反射。
        所以这里没有评分、没有打卡、没有排行榜——只有一群会等你把话说完的语伴。
      </p>
    </section>
  );
}

/* ---------------------------------------------------------------- 小组件 */

function SectionHead({ title, desc }: { title: string; desc: string }) {
  return (
    <div className="w4-settings-section-head">
      <h2 className="w4-settings-section-title">{title}</h2>
      <p className="w4-settings-section-desc">{desc}</p>
    </div>
  );
}

function SettingRow({
  title,
  desc,
  control,
}: {
  title: string;
  desc?: string;
  control?: ReactNode;
}) {
  return (
    <div className="w4-settings-row">
      <div className="w4-settings-row-copy">
        <div className="w4-settings-row-title">{title}</div>
        {desc ? <div className="w4-settings-row-desc">{desc}</div> : null}
      </div>
      {control ? <div className="w4-settings-row-control">{control}</div> : null}
    </div>
  );
}
