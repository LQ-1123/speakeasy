/**
 * 酒馆（SillyTavern）预设 → 文风候选提取。
 *
 * 支持三种容器：
 * - 标准预设：`{ "prompts": [ {identifier, name, content, enabled}, ... ] }`
 * - 纯数组：`[ {...}, ... ]`
 * - 数据银行：`{ "entries": { "0": {content}, ... } }`
 *
 * 重点兼容「Sudachi-Next」这类预设：文风不在 `jailbreak`（那里只有
 * `{{getvar::style}}`），而是分散在 `{{setvar::style:: ... }}` 条目里。
 * 所以这里既认「名字像文风」，也认「内容里给 style 赋值」。
 */

export interface TavernStyleCandidate {
  name: string;
  text: string;
}

export interface TavernStyleExtraction {
  candidates: TavernStyleCandidate[];
  error?: string;
}

interface RawEntry {
  identifier?: unknown;
  name?: unknown;
  content?: unknown;
  enabled?: unknown;
}

const STYLE_NAME_RE = /文风|写法|笔法|行文|叙述|style|writing|narrat|author|writer/i;
const SETVAR_STYLE_RE = /\{\{\s*setvar::style::([\s\S]*?)\}\}/;

function entryList(data: unknown): RawEntry[] | null {
  if (Array.isArray(data)) return data as RawEntry[];
  if (!data || typeof data !== "object") return null;
  const obj = data as { prompts?: unknown; entries?: unknown };
  if (Array.isArray(obj.prompts)) return obj.prompts as RawEntry[];
  if (obj.entries && typeof obj.entries === "object") {
    return Object.values(obj.entries as Record<string, unknown>).filter(
      (item): item is RawEntry => Boolean(item) && typeof item === "object",
    );
  }
  return null;
}

function cleanStyleText(content: string): string | null {
  // 去掉酒馆注释 {{// ... }}（可能跨行）
  let text = content.replace(/\{\{\/\/[\s\S]*?\}\}/g, "").trim();

  const setvar = text.match(SETVAR_STYLE_RE);
  if (setvar) {
    text = setvar[1].trim();
  } else if (/setvar::style/.test(text)) {
    // 形如多段 setvar 拼接、但正则没匹配到：放弃，避免把变量脚本塞进 prompt
    return null;
  }

  // 形如 <style>...</style> 的包裹去掉标签本身
  const wrapped = text.match(/^<style>\s*([\s\S]*?)\s*<\/style>$/i);
  if (wrapped) text = wrapped[1].trim();

  // {{getvar::style}} 是「把变量插到这里」的模板，不是文风内容
  if (/getvar::style/.test(text)) return null;

  // 太短的多半是占位/标题/纯变量指令，不是可用文风（真实条目通常 100+ 字）
  if (text.length < 40) return null;
  // 去掉宏/标记后几乎没内容的，是 {{getvar::style}} 这类模板，不是文风本身
  const meaningful = text
    .replace(/\{\{[\s\S]*?\}\}/g, "")
    .replace(/[#<>`*_\-\s]/g, "");
  if (meaningful.length < 20) return null;
  if (!/[A-Za-z\u4e00-\u9fa5]/.test(text)) return null;
  return text;
}

export function extractTavernStyleCandidates(raw: string): TavernStyleExtraction {
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return { candidates: [], error: "不是合法 JSON" };
  }
  const list = entryList(data);
  if (!list) {
    return { candidates: [], error: "没找到 prompts / entries 数组（请粘贴酒馆预设 JSON）" };
  }

  const candidates: TavernStyleCandidate[] = [];
  const seen = new Set<string>();
  for (const entry of list) {
    if (!entry || typeof entry !== "object") continue;
    const content = typeof entry.content === "string" ? entry.content : "";
    if (!content.trim()) continue;
    // 酒馆里多个文风是「互斥选项」，靠 setvar 切换，通常 enabled=false；
    // 这类条目仍要作为候选列出来，否则 Sudachi 一条都提不到。
    const hasStyleVar = /setvar::style/.test(content);
    if (entry.enabled === false && !hasStyleVar) continue;

    const name = String(entry.name || entry.identifier || "").trim() || "未命名文风";
    const isStyleByName = STYLE_NAME_RE.test(name) || hasStyleVar;
    if (!isStyleByName) continue;

    const text = cleanStyleText(content);
    if (!text) continue;
    if (seen.has(text)) continue;
    seen.add(text);
    candidates.push({ name, text });
  }

  if (!candidates.length) {
    return { candidates: [], error: "没有识别到文风条目（名字或内容里要有 style/文风）" };
  }
  return { candidates };
}
