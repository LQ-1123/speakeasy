"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { AuthScreen } from "./AuthScreen";
import { AUTH_EXPIRED_EVENT, AUTH_LOGOUT_EVENT, clearSession, loadAuthConfig, restoreAccountSession, subscribeAccountChanges } from "./session";
import { cleanupAccountClient } from "./logoutFlow";

/** Business providers are mounted only after the Cookie/native session is restored. */
export function AuthGate({ children, preview = false }: { children?: ReactNode; preview?: boolean }) {
  const [status, setStatus] = useState<"restoring" | "login" | "ready" | "error">(preview ? "ready" : "restoring");
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const cleaning = useRef(false);
  useEffect(() => {
    if (preview) return;
    let alive = true;
    let generation = 0;
    const expired = () => {
      generation += 1;
      setStatus("login");
      if (cleaning.current) return;
      cleaning.current = true;
      clearSession();
      cleanupAccountClient();
      cleaning.current = false;
    };
    window.addEventListener(AUTH_EXPIRED_EVENT, expired);
    window.addEventListener(AUTH_LOGOUT_EVENT, expired);
    const unsubscribeChanges = subscribeAccountChanges(expired, () => {
      generation += 1;
      clearSession();
      cleanupAccountClient();
      setStatus("restoring");
      setRetry((value) => value + 1);
    });
    const attempt = generation;
    void (async () => {
      try {
        const config = await loadAuthConfig();
        if (!alive || generation !== attempt) return;
        if (!config.email_code_auth) { setStatus("ready"); return; }
        const restored = await restoreAccountSession();
        if (alive && generation === attempt) setStatus(restored ? "ready" : "login");
      } catch (err) {
        if (alive && generation === attempt) { setError(err instanceof Error ? err.message : "无法连接，请重试"); setStatus("error"); }
      }
    })();
    return () => { alive = false; unsubscribeChanges(); window.removeEventListener(AUTH_EXPIRED_EVENT, expired); window.removeEventListener(AUTH_LOGOUT_EVENT, expired); };
  }, [retry, preview]);
  if (status === "ready") return <>{children}</>;
  if (status === "login") return <AuthScreen onAuthenticated={() => { setError(""); setStatus("ready"); }} />;
  return <div role="status" style={{ padding: 32, textAlign: "center" }}>{status === "restoring" ? "正在恢复登录…" : <><p>{error}</p><button type="button" onClick={() => { setStatus("restoring"); setRetry((value) => value + 1); }}>重新连接</button></>}</div>;
}
