import { expect, it } from "vitest";

import {
  authNotice,
  describeAuthFailure,
  hasErrors,
  looksLikeAuthRequired,
  normalizeEmail,
  validateAuthForm,
  validateEmail,
  validatePassword,
} from "./authFlow";

it("validates email and normalizes case/space", () => {
  expect(normalizeEmail("  A@Example.COM ")).toBe("a@example.com");
  expect(validateEmail("a@example.com")).toBe("");
  expect(validateEmail("")).toContain("请输入邮箱");
  expect(validateEmail("not-an-email")).toContain("格式");
  expect(validateEmail("a@b")).toContain("格式");
});

it("password rules depend on mode", () => {
  expect(validatePassword("", "login")).toContain("请输入密码");
  expect(validatePassword("short", "login")).toBe(""); // 登录只要非空
  expect(validatePassword("short", "register")).toContain("8 位");
  expect(validatePassword("goodpass1", "register")).toBe("");
  expect(validatePassword("x".repeat(129), "register")).toContain("过长");
});

it("form validation collects field errors", () => {
  const errors = validateAuthForm("register", {
    email: "bad",
    password: "123",
    confirm: "1234",
    inviteCode: "",
  });
  expect(hasErrors(errors)).toBe(true);
  expect(errors.email).toBeTruthy();
  expect(errors.password).toBeTruthy();
  expect(errors.confirm).toContain("不一致");

  expect(
    hasErrors(
      validateAuthForm("register", {
        email: "a@example.com",
        password: "goodpass1",
        confirm: "goodpass1",
        inviteCode: "",
      }),
    ),
  ).toBe(false);

  // 找回密码只校验邮箱
  expect(hasErrors(validateAuthForm("forgot", { email: "", password: "", confirm: "", inviteCode: "" }))).toBe(false);
});

it("maps server failures to friendly copy", () => {
  expect(describeAuthFailure("邀请码无效或已用尽")).toContain("邀请码");
  expect(describeAuthFailure("邮箱已被注册")).toContain("已注册");
  expect(describeAuthFailure("邮箱或密码不正确")).toContain("密码不对");
  expect(describeAuthFailure("尝试次数过多，请稍后再试")).toContain("15 分钟");
  expect(describeAuthFailure("")).toContain("网络异常");
  expect(describeAuthFailure("某个未知错误")).toBe("某个未知错误");
});

it("describes post-register notices and auth-required detection", () => {
  expect(authNotice({ created: true, dev_verify_token: "tok" })).toContain("tok");
  expect(authNotice({ created: true })).toContain("验证");
  expect(authNotice({ dev_reset_token: "r" })).toContain("r");
  expect(authNotice({})).toBe("");
  expect(looksLikeAuthRequired("未登录")).toBe(true);
  expect(looksLikeAuthRequired("会话已失效，请重新登录")).toBe(true);
  expect(looksLikeAuthRequired("其他错误")).toBe(false);
});
