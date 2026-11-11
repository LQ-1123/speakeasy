/**
 * 管理台 API 客户端：统一 fetch、CSRF、错误格式化。
 *
 * 设计要点
 * --------
 * 1. **CSRF**：非 GET 请求自动带 `X-Admin-CSRF`。令牌来自 `GET /api/admin/session`，
 *    首请求前懒加载并缓存；收到 403 时失效重取一次（服务端密钥轮换/会话过期）。
 * 2. **错误格式统一**：422 取 `issues[]`（后端校验失败的标准形状），
 *    其余取 `detail`；都没有则用状态码兜底。调用方只需 `catch (e: AdminApiError)`。
 * 3. **登录跳转**：401/403 且判定为"未登录"时跳 `/admin`（服务端会返回登录页），
 *    而不是在控制台里弹一个无解的空白。
 */

/** 后端校验失败（422）或其它 HTTP 错误统一成这个类型。 */
export class AdminApiError extends Error {
  readonly status: number;
  readonly issues: string[];
  readonly detail: string;

  constructor(status: number, detail: string, issues: string[] = []) {
    super(issues.length ? issues.join("；") : detail || `HTTP ${status}`);
    this.name = "AdminApiError";
    this.status = status;
    this.detail = detail;
    this.issues = issues;
  }

  /** 是否是"没登录/被拒"类错误（调用方用来决定跳登录）。 */
  get isAuthError(): boolean {
    return this.status === 401 || this.status === 403;
  }
}

export interface AdminSession {
  authenticated: boolean;
  /** CSRF 令牌；为空字符串表示服务端尚未启用（或未配口令的本机直通）。 */
  csrf: string;
  password_configured: boolean;
  version?: string;
}

let csrfToken = "";
let sessionPromise: Promise<AdminSession> | null = null;

function originUrl(path: string): string {
  // 管理台与 API 同源：用相对路径，静态导出下换端口/域名都不用改代码。
  return path.startsWith("/") ? path : `/${path}`;
}

/** 读取会话（含 CSRF 令牌）。并发调用共享同一个请求。 */
export async function getSession(force = false): Promise<AdminSession> {
  if (!force && sessionPromise) return sessionPromise;
  sessionPromise = (async () => {
    const response = await fetch(originUrl("/api/admin/session"), {
      credentials: "include",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) {
      // 会话接口本身不该 500；真失败时降级成"未登录"，让上层去登录页。
      throw new AdminApiError(response.status, "无法读取管理会话");
    }
    const data = (await response.json()) as AdminSession;
    csrfToken = String(data.csrf || "");
    return data;
  })();
  try {
    return await sessionPromise;
  } catch (error) {
    sessionPromise = null;
    throw error;
  }
}

async function ensureCsrf(): Promise<string> {
  if (csrfToken) return csrfToken;
  try {
    const session = await getSession();
    return session.csrf || "";
  } catch {
    // 会话取不到时不要阻断请求——后端若要求 CSRF 会返回 403，
    // 那时再由 refreshAndRetry 处理，错误信息更准确。
    return "";
  }
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  /** 上传用：直接给 FormData，不设 Content-Type（浏览器自己加 boundary）。 */
  form?: FormData;
  signal?: AbortSignal;
  /** 内部用：已经重试过一次就不要再重试，避免死循环。 */
  __retried?: boolean;
}

function isMutation(method: string): boolean {
  return method !== "GET" && method !== "HEAD";
}

async function parseError(response: Response): Promise<AdminApiError> {
  let detail = "";
  let issues: string[] = [];
  try {
    const data = (await response.json()) as { detail?: unknown; issues?: unknown; message?: unknown };
    if (Array.isArray(data.issues)) {
      issues = data.issues.map((item) => String(item));
    }
    if (typeof data.detail === "string") {
      detail = data.detail;
    } else if (Array.isArray(data.detail)) {
      // FastAPI 原生 422 形状：[{loc, msg, type}]
      issues = [
        ...issues,
        ...data.detail.map((item) => {
          const row = item as { loc?: unknown; msg?: unknown };
          const loc = Array.isArray(row.loc) ? row.loc.join(".") : "";
          const msg = typeof row.msg === "string" ? row.msg : JSON.stringify(item);
          return loc ? `${loc}: ${msg}` : msg;
        }),
      ];
    } else if (typeof data.message === "string") {
      detail = data.message;
    }
  } catch {
    // 非 JSON 响应（如 HTML 登录页），保留状态码即可
  }
  if (!detail && !issues.length) detail = response.statusText || `HTTP ${response.status}`;
  return new AdminApiError(response.status, detail, issues);
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json" };
  let body: BodyInit | undefined;

  if (options.form) {
    body = options.form;
  } else if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }

  if (isMutation(method)) {
    const csrf = await ensureCsrf();
    if (csrf) headers["X-Admin-CSRF"] = csrf;
  }

  const response = await fetch(originUrl(path), {
    method,
    credentials: "include",
    headers,
    body,
    signal: options.signal,
  });

  if (response.ok) {
    if (response.status === 204) return undefined as T;
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) return (await response.json()) as T;
    // 音频试听之类的二进制响应由调用方用 requestBlob
    return (await response.text()) as unknown as T;
  }

  const error = await parseError(response);

  // 403 可能是 CSRF 令牌过期（服务端换了密钥）；刷新令牌后重试一次。
  if (error.status === 403 && isMutation(method) && !options.__retried) {
    sessionPromise = null;
    csrfToken = "";
    await getSession(true).catch(() => undefined);
    return request<T>(path, { ...options, __retried: true });
  }

  throw error;
}

/** GET 便捷封装。 */
export function apiGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { method: "GET", signal });
}

/** 写操作便捷封装。 */
export function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: "POST", body });
}

export function apiPut<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: "PUT", body });
}

export function apiPatch<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: "PATCH", body });
}

export function apiDelete<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: "DELETE", body });
}

/** 上传（multipart/form-data）。 */
export function apiUpload<T>(path: string, form: FormData): Promise<T> {
  return request<T>(path, { method: "POST", form });
}

/**
 * 取二进制资源（音色试听等）。
 * 注意：不用 URL 传 token——管理台用 Cookie，fetch + blob 即可。
 */
export async function requestBlob(path: string): Promise<Blob> {
  const response = await fetch(originUrl(path), { credentials: "include" });
  if (!response.ok) throw await parseError(response);
  return response.blob();
}

/**
 * **写操作**取二进制资源（如 ``POST /voice-pool/preview`` 返回 mp3）。
 *
 * 为什么单独一个函数而不是让 `requestBlob` 带参数：
 * 写请求必须带 CSRF 头、且 403 时要刷新令牌重试一次——这套逻辑与
 * :func:`request` 完全一致。二进制响应不能走 `request`（它会尝试解析
 * JSON/文本），所以在同一套鉴权语义上另开一个返回 Blob 的出口。
 *
 * 之所以抽到这里：音色池与角色工坊都要试听，各自手写一份会漂移
 * （一份忘了带 CSRF、另一份忘了 403 重试，这类 bug 很难被发现）。
 */
export async function requestBlobPost(
  path: string,
  body?: unknown,
  options: { __retried?: boolean } = {},
): Promise<Blob> {
  const headers: Record<string, string> = { Accept: "audio/*, application/octet-stream" };
  let payload: BodyInit | undefined;
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }

  const csrf = await ensureCsrf();
  if (csrf) headers["X-Admin-CSRF"] = csrf;

  const response = await fetch(originUrl(path), {
    method: "POST",
    credentials: "include",
    headers,
    body: payload,
  });

  if (response.ok) return response.blob();

  const error = await parseError(response);
  // 403 可能是 CSRF 令牌过期；刷新令牌后重试一次（与 request() 一致）。
  if (error.status === 403 && !options.__retried) {
    sessionPromise = null;
    csrfToken = "";
    await getSession(true).catch(() => undefined);
    return requestBlobPost(path, body, { __retried: true });
  }
  throw error;
}

/** 退出登录：服务端清 Cookie 后整页跳回 /admin（会落到登录页）。 */
export async function logout(): Promise<void> {
  try {
    await fetch(originUrl("/admin/logout"), {
      method: "POST",
      credentials: "include",
      redirect: "manual",
    });
  } finally {
    csrfToken = "";
    sessionPromise = null;
    window.location.replace("/admin");
  }
}

/** 构造带查询参数的路径（跳过空值）。 */
export function withQuery(path: string, query: Record<string, string | number | boolean | undefined>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === "" || value === null) continue;
    params.set(key, String(value));
  }
  const search = params.toString();
  return search ? `${path}?${search}` : path;
}
