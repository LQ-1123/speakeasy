/**
 * 登录/注册/找回密码的纯逻辑（与 UI 解耦，便于单测）。
 *
 * 校验口径与后端保持一致：
 * - 邮箱：`x@y.z` 形式（后端 validate_email 同义）；
 * - 密码：注册/重置 ≥8 位；登录只要求非空（老账号可能有更短历史密码）；
 * - 邀请码：内测期由运营发放，前端只做「去除空格 + 非空校验」，是否有效由服务端判定。
 */

export type AuthMode = "login" | "register" | "forgot";

export interface AuthForm {
  email: string;
  password: string;
  confirm: string;
  inviteCode: string;
}

export interface FieldErrors {
  email?: string;
  password?: string;
  confirm?: string;
  inviteCode?: string;
  form?: string;
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

export function normalizeEmail(value: string): string {
  return (value ?? "").trim().toLowerCase();
}

export function validateEmail(value: string): string {
  const email = normalizeEmail(value);
  if (!email) return "请输入邮箱";
  if (!EMAIL_RE.test(email) || email.length > 254) return "邮箱格式不正确";
  return "";
}

export function validatePassword(value: string, mode: AuthMode): string {
  const password = value ?? "";
  if (!password) return "请输入密码";
  if (mode !== "login" && password.length < 8) return "密码至少 8 位";
  if (password.length > 128) return "密码过长";
  return "";
}

export function validateAuthForm(mode: AuthMode, form: AuthForm): FieldErrors {
  const errors: FieldErrors = {};
  if (mode === "forgot") return errors; // 找回密码只填邮箱
  const email = validateEmail(form.email);
  if (email) errors.email = email;
  const password = validatePassword(form.password, mode);
  if (password) errors.password = password;
  if (mode === "register" && (form.confirm ?? "") !== (form.password ?? "")) {
    errors.confirm = "两次输入的密码不一致";
  }
  return errors;
}

export function hasErrors(errors: FieldErrors): boolean {
  return Object.keys(errors).length > 0;
}

/** 把服务端错误文案映射成用户能看懂的提示（保留原文兜底）。 */
export function describeAuthFailure(message: string): string {
  const text = (message ?? "").trim();
  if (!text) return "网络异常，请稍后再试";
  if (text.includes("邀请码")) return "邀请码无效或已用完，请向运营领取";
  if (text.includes("邮箱已被注册") || text.includes("已注册")) {
    return "这个邮箱已注册，直接登录或找回密码即可";
  }
  if (text.includes("邮箱或密码不正确")) return "邮箱或密码不对，请重试";
  if (text.includes("次数过多")) return "尝试次数过多，请 15 分钟后再试";
  if (text.includes("邮箱格式")) return "邮箱格式不正确";
  if (text.includes("密码至少") || text.includes("密码长度")) return "密码至少 8 位";
  if (text.includes("会话") || text.includes("登录")) return "登录状态已失效，请重新登录";
  return text;
}

/** 注册/找回密码成功后的提示（含开发模式的调试令牌）。 */
export function authNotice(result: {
  created?: boolean;
  email_verified?: boolean;
  dev_verify_token?: string;
  dev_reset_token?: string;
}): string {
  if (result.dev_reset_token) {
    return `已发送。开发模式令牌：${result.dev_reset_token}`;
  }
  if (result.dev_verify_token) {
    return `注册成功，请验证邮箱。开发模式令牌：${result.dev_verify_token}`;
  }
  if (result.created) return "注册成功，请到邮箱完成验证";
  return "";
}

/** 服务端要求登录（SE_REQUIRE_AUTH=1）时自动弹登录页的判定。 */
export function looksLikeAuthRequired(message: string): boolean {
  const text = (message ?? "").trim();
  return text.includes("未登录") || text.includes("401") || text.includes("会话已失效");
}
