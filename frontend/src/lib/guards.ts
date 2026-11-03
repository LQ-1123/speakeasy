/** 运行时类型收敛工具：协议字段可能缺失/异构，全部经过这里，避免 `any` 扩散。 */

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function asString(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

/** 同上，但缺失时返回空串（用于必填展示字段，避免 `string | undefined`）。 */
export function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** 数值，缺失时返回 0。 */
export function num(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

export function asNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

export function asBoolean(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

export function asStringArray(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const out = value.filter((item): item is string => typeof item === "string" && item.length > 0);
  return out.length > 0 ? out : undefined;
}

/** 引擎字段既可能是字符串（"faster-whisper:..."）也可能是对象（{backend, ready}）。 */
export function asEngineLabel(value: unknown): string | undefined {
  if (typeof value === "string") return value.length > 0 ? value : undefined;
  if (isRecord(value)) {
    return (
      asString(value.backend) ??
      asString(value.name) ??
      asString(value.id) ??
      asString(value.model)
    );
  }
  return undefined;
}

export function errorMessageOf(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "string") return error;
  return "未知错误";
}

/**
 * 麦克风可用性预检（在真正调用 getUserMedia 之前跑）。
 *
 * 返回 null 表示看起来可以尝试；否则返回**可操作**的说明。
 * 预检不能保证成功（权限仍需用户授予），但能把"环境不允许"这类
 * 必然失败的情况提前说清楚，而不是抛一个笼统的 Permission denied。
 */
export function micPreflight(): string | null {
  if (typeof window === "undefined") return null;

  if (!window.isSecureContext) {
    const { protocol, hostname, port, href } = window.location;
    const suggest = `http://127.0.0.1:${port || 8000}`;
    return (
      `当前页面不是安全上下文（${protocol}//${hostname}）。` +
      `浏览器只在 https 或 localhost / 127.0.0.1 下允许麦克风。改用 ${suggest} 打开即可` +
      `（当前地址：${href.slice(0, 80)}）`
    );
  }

  if (!navigator.mediaDevices?.getUserMedia) {
    return "当前浏览器不支持麦克风采集。建议用 Chrome / Edge 最新版";
  }

  return null;
}

/**
 * 把 `getUserMedia` 抛出的错误翻译成**可操作的**中文指引。
 *
 * 浏览器原生的 `Permission denied` 太笼统：可能是用户点了拒绝、可能是
 * 站点权限被永久屏蔽、可能是 Windows 隐私开关、也可能是没有可用设备。
 * 每种情况的操作路径完全不同，所以分开说明。
 */
export function micErrorMessage(error: unknown): string {
  const raw = errorMessageOf(error);
  const name = isRecord(error) ? str(error.name) : error instanceof Error ? error.name : "";
  const key = `${name} ${raw}`.toLowerCase();

  // 1) 用户点了"阻止"，或站点权限被记住为"拒绝"
  if (key.includes("notallowed") || key.includes("permission denied") || key.includes("denied")) {
    return (
      "麦克风被拒绝。点地址栏左侧的🔒图标 → 找到「麦克风」→ 改成「允许」→ 刷新页面。" +
      "如果那里显示已允许，请检查 Windows 设置 → 隐私和安全性 → 麦克风 是否开启了「允许桌面应用访问」"
    );
  }

  // 2) 没有可用设备 / 设备被独占
  if (key.includes("notfound") || key.includes("devicesnotfound") || key.includes("requested device not found")) {
    return (
      "找不到可用的麦克风。检查设备是否插好，或在 设置 → 系统 → 声音 → 输入 里选一个设备。" +
      "另外本机有虚拟声卡（VB-Cable / Oculus / Steam），如果默认输入选错了会拿不到真实麦克风"
    );
  }

  // 3) 设备被其他程序独占
  if (key.includes("notreadable") || key.includes("trackstart") || key.includes("could not start")) {
    return "麦克风被其他程序占用了（会议软件 / 录音软件）。关掉它们再试";
  }

  // 4) 约束无法满足
  if (key.includes("overconstrained") || key.includes("constraint")) {
    return "当前麦克风不支持所需的音频参数。换一个输入设备试试";
  }

  // 5) 非安全上下文
  if (key.includes("secure context") || key.includes("insecure")) {
    return "浏览器只允许在 https 或 http://localhost / 127.0.0.1 下使用麦克风。请改用 http://127.0.0.1:8000";
  }

  // 6) 兜底：带上原始错误，便于排查
  return `${raw}。可点地址栏左侧🔒图标检查麦克风权限`;
}

