"use client";

/**
 * LLM 模型设置弹窗。
 *
 * 设计要点：
 * - Key 输入框是「只写不读」：后端永不回传明文，前端也只显示 `sk-a…2345` 指纹。
 * - 「保存并启用」会让后端真的发一次极小请求验证，因此能立刻知道 key 对不对。
 * - 预设下拉只是帮你填 base_url / model，key 仍需手填（安全上不能替你猜）。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertCircle, Check, Loader2, Server, Trash2, X, Zap } from "lucide-react";

import {
  fetchLlmConfig,
  resetLlmConfig,
  saveLlmConfig,
  testLlmConfig,
  type LlmConfigView,
  type LlmPreset,
  type LlmVerifyResult,
} from "@/lib/api";

interface LlmSettingsDialogProps {
  open: boolean;
  onClose: () => void;
  /** 保存成功后回调（父组件据此刷新"未配置"提示条） */
  onSaved?: () => void;
}

type Tone = "idle" | "busy" | "ok" | "err";

export function LlmSettingsDialog({ open, onClose, onSaved }: LlmSettingsDialogProps) {
  const [loading, setLoading] = useState(true);
  const [config, setConfig] = useState<LlmConfigView | null>(null);
  const [presets, setPresets] = useState<LlmPreset[]>([]);
  const [presetId, setPresetId] = useState("");

  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [temperature, setTemperature] = useState("");
  const [clearKey, setClearKey] = useState(false);

  const [tone, setTone] = useState<Tone>("idle");
  const [message, setMessage] = useState("");
  const [detail, setDetail] = useState("");
  const [busy, setBusy] = useState(false);

  const resetForm = useCallback((cfg: LlmConfigView | null) => {
    setBaseUrl(cfg?.base_url ?? "");
    setModel(cfg?.model ?? "");
    setTemperature(cfg?.temperature != null ? String(cfg.temperature) : "");
    setApiKey("");
    setClearKey(false);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    const payload = await fetchLlmConfig();
    if (payload.ok && payload.config) {
      setConfig(payload.config);
      setPresets(payload.presets);
      resetForm(payload.config);
      if (payload.config.configured && payload.config.api_key_set) {
        setTone("ok");
        setMessage(`当前已生效：${payload.config.model} @ ${payload.config.base_url}`);
        setDetail(
          payload.config.source === "file" ? "配置来源：界面保存" : "配置来源：.env",
        );
      } else {
        setTone("idle");
        setMessage("尚未配置真实模型，当前回复由本地规则引擎生成。");
        setDetail("");
      }
    } else {
      setTone("err");
      setMessage(`读取配置失败：${payload.error ?? "后端未启动"}`);
      setDetail("");
    }
    setLoading(false);
  }, [resetForm]);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  // Esc 关闭
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const applyPreset = (id: string) => {
    setPresetId(id);
    const p = presets.find((x) => x.id === id);
    if (!p) return;
    if (p.base_url) setBaseUrl(p.base_url);
    if (p.model) setModel(p.model);
    setTone("idle");
    setMessage(`已填入 ${p.name} 的默认端点与模型，填好 API Key 后点「保存并启用」。`);
    setDetail(p.note);
  };

  const buildPayload = () => {
    const out: Record<string, unknown> = {};
    if (baseUrl.trim()) out.base_url = baseUrl.trim();
    if (model.trim()) out.model = model.trim();
    if (temperature.trim() !== "") {
      const t = Number(temperature);
      if (Number.isFinite(t)) out.temperature = t;
    }
    if (clearKey) out.api_key = "__CLEAR__";
    else if (apiKey.trim()) out.api_key = apiKey.trim();
    return out;
  };

  const doTest = async () => {
    setBusy(true);
    setTone("busy");
    setMessage("正在测试连接…");
    setDetail("");
    const payload = buildPayload();
    const r: LlmVerifyResult = await testLlmConfig({
      base_url: payload.base_url as string | undefined,
      api_key: payload.api_key as string | undefined,
      model: payload.model as string | undefined,
    });
    setTone(r.ok ? "ok" : "err");
    setMessage(r.ok ? `连接成功（${Math.round(r.latency_ms)}ms）` : "连接失败");
    setDetail(r.ok ? `模型返回：${r.model_reply || "(空)"}` : r.message);
    setBusy(false);
  };

  const doSave = async () => {
    const payload = buildPayload();
    if (Object.keys(payload).length === 0) {
      setTone("err");
      setMessage("没有要保存的内容");
      setDetail("");
      return;
    }
    setBusy(true);
    setTone("busy");
    setMessage("保存并验证中…");
    setDetail("");
    const r = await saveLlmConfig({ ...payload, verify: true } as never);
    if (r.ok) {
      setTone("ok");
      setMessage(r.message);
      setDetail(
        r.verified
          ? `验证耗时 ${Math.round(r.verified.latency_ms)}ms，模型返回：${r.verified.model_reply || "(空)"}`
          : "",
      );
      setApiKey("");
      setClearKey(false);
      await load();
      onSaved?.();
    } else {
      setTone("err");
      setMessage(r.message);
      setDetail(r.verified?.message ?? "");
    }
    setBusy(false);
  };

  const doReset = async () => {
    setBusy(true);
    setTone("busy");
    setMessage("正在还原…");
    const r = await resetLlmConfig();
    setTone(r.ok ? "ok" : "err");
    setMessage(r.message);
    setDetail("");
    await load();
    onSaved?.();
    setBusy(false);
  };

  const toneStyle = useMemo(() => {
    switch (tone) {
      case "ok":
        return "border-[color:var(--accent-ok)] text-[color:var(--accent-ok)]";
      case "err":
        return "border-[color:var(--accent-danger)] text-[color:var(--accent-danger)]";
      case "busy":
        return "border-[color:var(--accent)] text-[color:var(--accent)]";
      default:
        return "border-[color:var(--line-strong)] text-[color:var(--text-dim)]";
    }
  }, [tone]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true">
      <button
        type="button"
        aria-label="关闭"
        onClick={onClose}
        className="absolute inset-0 bg-black/70 backdrop-blur-[2px]"
      />
      <div className="relative flex max-h-[90vh] w-full max-w-[560px] flex-col overflow-hidden rounded-[3px] border border-[color:var(--line-strong)] bg-[color:var(--surface)] shadow-2xl">
        {/* 头部 */}
        <div className="flex shrink-0 items-center gap-2.5 border-b border-[color:var(--line)] px-4 py-3">
          <Server size={14} className="text-[color:var(--accent)]" />
          <div className="min-w-0 flex-1">
            <h2 className="text-[13px] font-semibold text-[color:var(--text)]">文本模型设置</h2>
            <p className="mt-0.5 text-[9.5px] uppercase tracking-[0.14em] text-[color:var(--text-faint)]">
              LLM · OpenAI compatible
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="flex h-6 w-6 items-center justify-center rounded-[2px] text-[color:var(--text-faint)] transition-colors hover:text-[color:var(--text)]"
          >
            <X size={14} />
          </button>
        </div>

        {/* 主体 */}
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3.5">
          {loading ? (
            <div className="flex items-center justify-center gap-2 py-10 text-[11px] text-[color:var(--text-faint)]">
              <Loader2 size={13} className="animate-spin" />
              读取配置…
            </div>
          ) : (
            <>
              {/* 状态条 */}
              {message && (
                <div className={`mb-3.5 rounded-[2px] border px-3 py-2 ${toneStyle}`}>
                  <div className="flex items-start gap-1.5">
                    {tone === "ok" ? (
                      <Check size={11} className="mt-0.5 shrink-0" />
                    ) : tone === "busy" ? (
                      <Loader2 size={11} className="mt-0.5 shrink-0 animate-spin" />
                    ) : (
                      <AlertCircle size={11} className="mt-0.5 shrink-0" />
                    )}
                    <span className="min-w-0 flex-1 whitespace-pre-wrap break-words text-[10.5px] leading-relaxed">
                      {message}
                    </span>
                  </div>
                  {detail && (
                    <p className="mt-1 whitespace-pre-wrap break-words pl-[18px] text-[9.5px] leading-relaxed opacity-75">
                      {detail}
                    </p>
                  )}
                </div>
              )}

              {/* 预设 */}
              <Field label="快速预设">
                <select
                  value={presetId}
                  onChange={(e) => applyPreset(e.target.value)}
                  className="w-full rounded-[2px] border border-[color:var(--line-strong)] bg-[color:var(--surface-2)] px-2.5 py-2 text-[11.5px] text-[color:var(--text)] outline-none focus:border-[color:var(--accent)]"
                >
                  <option value="">— 选择供应商（自动填端点与模型名）—</option>
                  {presets.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                      {p.note ? ` ·  ${p.note}` : ""}
                    </option>
                  ))}
                </select>
              </Field>

              {/* Base URL */}
              <Field
                label="Base URL"
                hint={
                  <>
                    必须含版本路径，例：<code className="rounded bg-[color:var(--surface-2)] px-1">https://api.deepseek.com/v1</code>
                  </>
                }
              >
                <input
                  type="text"
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                  placeholder="https://api.deepseek.com/v1"
                  spellCheck={false}
                  className="mono w-full rounded-[2px] border border-[color:var(--line-strong)] bg-[color:var(--surface-2)] px-2.5 py-2 text-[11.5px] text-[color:var(--text)] outline-none focus:border-[color:var(--accent)]"
                />
              </Field>

              {/* API Key */}
              <Field
                label={
                  <>
                    API Key
                    {config?.api_key_set ? (
                      <span className="ml-1.5 rounded-[2px] bg-[color:var(--accent-dim)] px-1.5 py-0.5 text-[8.5px] font-medium text-[color:var(--accent-ok)]">
                        已设置 {config.api_key_masked}
                      </span>
                    ) : (
                      <span className="ml-1.5 rounded-[2px] bg-[color:var(--surface-2)] px-1.5 py-0.5 text-[8.5px] font-medium text-[color:var(--accent-danger)]">
                        未设置
                      </span>
                    )}
                  </>
                }
                hint="只保存在本机 data/llm_config.json，接口永不回传明文。本地 Ollama 可留空。"
              >
                <div className="flex gap-1.5">
                  <input
                    type="password"
                    value={apiKey}
                    onChange={(e) => {
                      setApiKey(e.target.value);
                      setClearKey(false);
                    }}
                    placeholder={config?.api_key_set ? "留空则不修改已保存的密钥" : "粘贴密钥，如 sk-..."}
                    autoComplete="off"
                    spellCheck={false}
                    className="mono min-w-0 flex-1 rounded-[2px] border border-[color:var(--line-strong)] bg-[color:var(--surface-2)] px-2.5 py-2 text-[11.5px] text-[color:var(--text)] outline-none focus:border-[color:var(--accent)]"
                  />
                  <button
                    type="button"
                    onClick={() => {
                      setClearKey(true);
                      setApiKey("");
                      setTone("idle");
                      setMessage("已标记清除密钥，点「保存并启用」后生效。");
                      setDetail("");
                    }}
                    title="清除已保存的密钥"
                    className="shrink-0 rounded-[2px] border border-[color:var(--line-strong)] px-2 text-[color:var(--text-faint)] transition-colors hover:border-[color:var(--accent-danger)] hover:text-[color:var(--accent-danger)]"
                  >
                    <Trash2 size={11} />
                  </button>
                </div>
                {clearKey && (
                  <p className="mt-1 text-[9.5px] text-[color:var(--accent-warn)]">
                    将清除已保存的密钥
                  </p>
                )}
              </Field>

              {/* 模型名 */}
              <Field label="模型名">
                <input
                  type="text"
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  placeholder="deepseek-chat"
                  spellCheck={false}
                  className="mono w-full rounded-[2px] border border-[color:var(--line-strong)] bg-[color:var(--surface-2)] px-2.5 py-2 text-[11.5px] text-[color:var(--text)] outline-none focus:border-[color:var(--accent)]"
                />
              </Field>

              {/* 温度 */}
              <Field label="温度（可选）" hint="0 = 稳定，1 = 灵活。留空用默认 0.85。">
                <input
                  type="number"
                  value={temperature}
                  onChange={(e) => setTemperature(e.target.value)}
                  min={0}
                  max={2}
                  step={0.05}
                  placeholder="0.85"
                  className="mono w-full rounded-[2px] border border-[color:var(--line-strong)] bg-[color:var(--surface-2)] px-2.5 py-2 text-[11.5px] text-[color:var(--text)] outline-none focus:border-[color:var(--accent)]"
                />
              </Field>
            </>
          )}
        </div>

        {/* 底部操作 */}
        <div className="flex shrink-0 items-center gap-2 border-t border-[color:var(--line)] px-4 py-2.5">
          <button
            type="button"
            onClick={doTest}
            disabled={busy || loading}
            className="flex items-center gap-1.5 rounded-[2px] border border-[color:var(--line-strong)] bg-[color:var(--surface-2)] px-2.5 py-1.5 text-[11px] text-[color:var(--text-dim)] transition-colors hover:border-[color:var(--accent-line)] hover:text-[color:var(--accent)] disabled:opacity-40"
          >
            <Zap size={11} />
            测试连接
          </button>
          <button
            type="button"
            onClick={doSave}
            disabled={busy || loading}
            className="flex items-center gap-1.5 rounded-[2px] border border-[color:var(--accent-line)] bg-[color:var(--accent-dim)] px-3 py-1.5 text-[11px] font-medium text-[color:var(--accent)] transition-colors hover:bg-[color:var(--accent)]/20 disabled:opacity-40"
          >
            {busy ? <Loader2 size={11} className="animate-spin" /> : <Check size={11} />}
            保存并启用
          </button>
          <button
            type="button"
            onClick={doReset}
            disabled={busy || loading}
            className="ml-auto rounded-[2px] px-2 py-1.5 text-[10.5px] text-[color:var(--text-faint)] transition-colors hover:text-[color:var(--text-dim)] disabled:opacity-40"
            title="删除界面保存的配置，回到 .env"
          >
            还原 .env
          </button>
        </div>
      </div>
    </div>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: React.ReactNode;
  hint?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="mb-3">
      <label className="mb-1 block text-[9.5px] font-medium uppercase tracking-[0.12em] text-[color:var(--text-faint)]">
        {label}
      </label>
      {children}
      {hint && (
        <p className="mt-1 text-[9.5px] leading-relaxed text-[color:var(--text-faint)]">{hint}</p>
      )}
    </div>
  );
}
