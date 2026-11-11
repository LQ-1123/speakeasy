/**
 * 账号门（**当前首版已停用，文件保留待恢复**）。
 *
 * 2026-11 起产品改为免登录：多用户靠设备身份隔离，唯一门槛是首次用户信息引导
 * （`Onboarding`）。`App.tsx` 不再挂载本组件；需要恢复邮箱/密码登录时，
 * 把 `<AuthGate />` 加回 `App.tsx` 即可，后端接口与测试都还在。
 *
 * 原始行为：
 * - 服务端要求登录（SE_REQUIRE_AUTH=1）且本地没有会话时自动弹登录；
 * - 其他情况在右下角保留一个入口（内测用户可主动登录/注册）；
 * - 手动：`sonder:open-auth` 事件；URL：`?login=1`。
 */

import { useEffect, useState } from "react";

import { AuthScreen } from "./AuthScreen";
import { looksLikeAuthRequired } from "./authFlow";
import { authIdentity, hasSession } from "./session";

export function AuthGate() {
  const [open, setOpen] = useState(() => {
    if (typeof window === "undefined") return false;
    try {
      return new URLSearchParams(window.location.search).get("login") === "1";
    } catch {
      return false;
    }
  });
  const [loggedIn, setLoggedIn] = useState(() => hasSession());

  useEffect(() => {
    const onOpen = () => setOpen(true);
    window.addEventListener("sonder:open-auth", onOpen);
    if (!hasSession()) {
      void (async () => {
        try {
          await authIdentity();
        } catch (error) {
          if (looksLikeAuthRequired(error instanceof Error ? error.message : String(error))) {
            setOpen(true);
          }
        }
      })();
    }
    return () => window.removeEventListener("sonder:open-auth", onOpen);
  }, []);

  if (open) {
    return (
      <AuthScreen
        onClose={() => setOpen(false)}
        onAuthenticated={() => {
          setLoggedIn(true);
          setOpen(false);
          /* 账号切换会改变数据隔离范围：整页重载最稳妥 */
          if (typeof window !== "undefined") window.location.reload();
        }}
      />
    );
  }
  if (loggedIn) return null;
  return (
    <button
      type="button"
      onClick={() => setOpen(true)}
      style={{
        position: "fixed",
        right: 14,
        bottom: 74,
        zIndex: 55,
        padding: "8px 14px",
        borderRadius: 999,
        border: "1px solid rgba(120,120,120,.3)",
        background: "var(--w4-surface, #fff)",
        color: "inherit",
        fontSize: 12,
        cursor: "pointer",
        boxShadow: "0 6px 20px rgba(0,0,0,.14)",
      }}
    >
      登录 / 注册
    </button>
  );
}
