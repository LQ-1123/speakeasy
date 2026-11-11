"use client";

/**
 * 设置 · 账号（**当前首版已停用，文件保留待恢复**）。
 *
 * 2026-11 起产品免登录：设置页不再展示「账号」分区；多用户靠设备身份隔离，
 * 首次进入只走 `Onboarding` 用户信息引导。恢复邮箱/密码登录时，把本组件
 * 重新挂回 `SettingsPane` 即可（`session.ts` / `accountClient.ts` 与后端接口都还在）。
 */

import { useCallback, useEffect, useState } from "react";
import { LogOut, ShieldCheck, UserRound } from "lucide-react";

import { adStatus, setAdsOptIn, teenStatus, type AdStatus, type TeenStatus } from "../v3/accountClient";
import {
  authIdentity,
  hasSession,
  listSessions,
  readSession,
  revokeSession,
  type AuthIdentity,
  type AuthSessionRow,
} from "../v3/session";
import { performLogout } from "../v3/logoutFlow";

function openAuth() {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent("sonder:open-auth"));
}

const cardStyle: React.CSSProperties = {
  display: "grid",
  gap: 10,
  padding: "14px 16px",
  borderRadius: 14,
  border: "1px solid rgba(120,120,120,.18)",
};

const buttonStyle: React.CSSProperties = {
  minHeight: 36,
  padding: "0 14px",
  borderRadius: 10,
  border: "1px solid rgba(120,120,120,.28)",
  background: "transparent",
  color: "inherit",
  fontSize: 13,
  cursor: "pointer",
};

const primaryButtonStyle: React.CSSProperties = {
  ...buttonStyle,
  border: "none",
  background: "var(--accent, #4f7cff)",
  color: "#fff",
  fontWeight: 600,
};

const muted: React.CSSProperties = { fontSize: 12, opacity: 0.72, lineHeight: 1.5 };

export function AccountSection() {
  const [signedIn, setSignedIn] = useState(() => hasSession());
  const session = readSession();
  const [identity, setIdentity] = useState<AuthIdentity | null>(null);
  const [sessions, setSessions] = useState<AuthSessionRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [ads, setAds] = useState<AdStatus | null>(null);
  const [teen, setTeen] = useState<TeenStatus | null>(null);

  const refresh = useCallback(async () => {
    if (!hasSession()) {
      setSignedIn(false);
      return;
    }
    setSignedIn(true);
    try {
      const [who, rows] = await Promise.all([authIdentity(), listSessions()]);
      setIdentity(who);
      setSessions(rows.items ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
    try {
      setAds(await adStatus());
    } catch {
      setAds(null);
    }
    try {
      setTeen(await teenStatus());
    } catch {
      setTeen(null);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function signOut(all: boolean) {
    setBusy(true);
    setError("");
    try {
      /* 退出会清理本地缓存/音频/事件游标，并 reload 断开事件连接、重绑推送 */
      await performLogout(all);
      setSignedIn(false);
      setIdentity(null);
      setSessions([]);
      if (typeof window !== "undefined") window.location.reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function revoke(sessionId: string) {
    setBusy(true);
    try {
      await revokeSession(sessionId);
      setSessions((prev) => prev.filter((row) => row.session_id !== sessionId));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function toggleAds(next: boolean) {
    setBusy(true);
    try {
      await setAdsOptIn(next);
      setAds(await adStatus());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  if (!signedIn) {
    return (
      <div style={{ display: "grid", gap: 14 }}>
        <div style={cardStyle}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 600 }}>
            <UserRound size={18} strokeWidth={1.8} />
            邮箱账号（内测）
          </div>
          <p style={muted}>
            登录后：多设备同步、可撤销设备会话、数据隔离（动态/私聊情绪/人物关系互不可见）。
            内测注册需要邀请码。
          </p>
          <button type="button" style={primaryButtonStyle} onClick={openAuth}>
            登录 / 注册
          </button>
        </div>
        {teen && teen.access && teen.access.allowed === false ? (
          <div style={cardStyle}>
            <div style={{ fontWeight: 600 }}>青少年模式</div>
            <p style={muted}>{teenReasonText(teen)}</p>
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <div style={cardStyle}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 600 }}>
          <UserRound size={18} strokeWidth={1.8} />
          {session?.email || identity?.email || "已登录"}
        </div>
        <p style={muted}>
          用户 ID：{session?.user_id || identity?.user_id || "-"}
          {identity?.email_verified ? " · 邮箱已验证" : " · 邮箱未验证"}
          {identity?.age ? ` · 年龄组 ${ageLabel(identity.age.group)}` : ""}
        </p>
        {teen && teen.access && teen.access.allowed === false ? (
          <p style={{ ...muted, color: "#c26a00" }}>{teenReasonText(teen)}</p>
        ) : null}
        {teen && teen.access && teen.access.reminder ? (
          <p style={{ ...muted, color: "#c26a00" }}>
            已连续使用 30 分钟，休息一下再继续吧。
          </p>
        ) : null}
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button type="button" style={buttonStyle} disabled={busy} onClick={() => void signOut(false)}>
            <LogOut size={14} style={{ verticalAlign: "-2px", marginRight: 6 }} />
            退出登录
          </button>
          <button type="button" style={buttonStyle} disabled={busy} onClick={() => void signOut(true)}>
            退出全部设备
          </button>
        </div>
        {error ? <p style={{ ...muted, color: "#d33" }}>{error}</p> : null}
      </div>

      <div style={cardStyle}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 600 }}>
          <ShieldCheck size={18} strokeWidth={1.8} />
          设备会话
        </div>
        {sessions.length === 0 ? (
          <p style={muted}>暂无可管理的会话。</p>
        ) : (
          <div style={{ display: "grid", gap: 8 }}>
            {sessions.map((row) => (
              <div
                key={row.session_id}
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 10,
                  fontSize: 13,
                }}
              >
                <span style={{ opacity: 0.82 }}>
                  {row.platform || row.device_key || "未知设备"} · {row.created_at?.slice(0, 10)}
                  {row.current ? " · 当前会话" : ""}
                </span>
                {row.current ? null : (
                  <button
                    type="button"
                    style={buttonStyle}
                    disabled={busy}
                    onClick={() => void revoke(row.session_id)}
                  >
                    撤销
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      <div style={cardStyle}>
        <div style={{ fontWeight: 600 }}>合作推荐</div>
        <p style={muted}>
          仅成年且已核验的账号可开启；默认关闭，随时可以关掉。开启后聊天与动态里可能看到
          标注「广告 · 品牌合作」的内容，关闭或举报会屏蔽该活动 30 天。
        </p>
        {ads ? (
          ads.eligible || ads.ads_opt_in ? (
            <label style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 13 }}>
              <input
                type="checkbox"
                checked={Boolean(ads.ads_opt_in)}
                disabled={busy || !ads.eligible}
                onChange={(event) => void toggleAds(event.target.checked)}
              />
              接收合作推荐
              {ads.eligible ? "" : `（${adReasonText(ads.reason)}）`}
            </label>
          ) : (
            <p style={muted}>当前账号不可开启：{adReasonText(ads.reason)}</p>
          )
        ) : (
          <p style={muted}>登录后可查看。</p>
        )}
      </div>
    </div>
  );
}

function ageLabel(group: string): string {
  if (group === "adult") return "成年";
  if (group === "teen") return "14–17 岁";
  return "待核验";
}

function teenReasonText(status: TeenStatus): string {
  const reason = status.access?.reason || "";
  if (reason === "age_unverified") return "请先完成年龄核验（可联系运营）。";
  if (reason === "guardian_required") return "14–17 岁需要先完成监护人绑定。";
  if (reason === "curfew") return "青少年模式：22:00–06:00 暂停使用，明早再来。";
  if (reason === "daily_limit") return "今日使用时间已用完，明天再来吧。";
  if (reason === "persona_blocked") return "监护人限制了该人物的访问。";
  return "青少年模式已开启。";
}

function adReasonText(reason: string): string {
  if (reason === "no_account") return "需要登录邮箱账号";
  if (reason === "ads_opt_out") return "未开启";
  if (reason === "not_adult_verified") return "仅已核验成年账号可用";
  if (reason === "ads_not_allowed") return "当前账号不可用该能力";
  return reason || "不可用";
}
