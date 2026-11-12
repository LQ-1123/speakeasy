import React from "react";
import { renderToString } from "react-dom/server";
import { expect, it, vi } from "vitest";
import { AuthGate } from "./AuthGate";
import { AuthScreen } from "./AuthScreen";
vi.stubGlobal("React", React);

it("does not mount business providers before configuration and session restoration", () => {
  let booted = false;
  function Business() { booted = true; return <div>private records</div>; }
  const html = renderToString(<AuthGate><Business /></AuthGate>);
  expect(booted).toBe(false);
  expect(html).not.toContain("private records");
  expect(html).toContain("正在恢复登录");
});

it("starts with email code login and does not ask for an invite before verification", () => {
  const html = renderToString(<AuthScreen />);
  expect(html).toContain("发送验证码");
  expect(html).not.toContain('type="password"');
  expect(html).not.toContain('id="auth-invite"');
});
