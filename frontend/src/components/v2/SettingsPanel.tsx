"use client";

/**
 * 个人设置（全屏）：对话模型 LLM + 语音识别 STT。
 *
 * 契约：docs/V2_STT.md §3/§5。
 * - 密钥输入框**永远为空**，只展示后端返回的脱敏指纹（`sk-a…2345`）；
 *   留空 = 不覆盖，`__CLEAR__` = 清除。
 * - 保存成功后回调 `onChanged`，父级重新拉 bootstrap（「演示模式」徽章随之更新）。
 * - STT 接口在后端尚未就绪（404）时显示「接口未就绪」空态与默认值，不白屏。
 */

import { useCallback, useEffect, useState, type ReactNode } from "react";

import { TTSSettingsSection } from "@/components/v2/TTSSettingsSection";
import {
  AlertCircle,
  CheckCircle2,
  ChevronLeft,
  Cloud,
  Cpu,
  KeyRound,
  Loader2,
  Mic,
  RefreshCw,
  RotateCcw,
  Save,
  Server,
  Settings2,
  Sparkles,
  Trash2,
  Zap,
} from "lucide-react";

import { useToast } from "@/components/v2/Toast";
import { ErrorBanner, Spinner } from "@/components/v2/ui";
import { ApiError, apiErrorText, getLlmSettings, getSttSettings, resetLlmSettings, resetSttSettings, saveLlmSettings, saveSttSettings, testLlmSettings, testSttSettings } from "@/lib/v2/api";
import { relativeLabel } from "@/lib/v2/format";
import {
  DEFAULT_STT_CONFIG,
  DEFAULT_STT_PRESETS,
  type LlmConfigView,
  type LlmHealthView,
  type LlmPreset,
  type LlmSaveInput,
  type SttConfigView,
  type SttHealthView,
  type SttPreset,
  type SttProvider,
} from "@/lib/v2/types";

interface SettingsPanelProps {
  onClose: () => void;
  /** 保存/重置成功后回调：父级重新拉 bootstrap，让全局状态同步 */
  onChanged?: () => void;
}

interface Feedback {
  tone: "ok" | "err" | "info";
  text: string;
  detail?: string;
}

type LlmStatus = "unknown" | "ok" | "mock" | "off" | "err";

const STT_PROVIDERS: Array<{ id: SttProvider; name: string; desc: string; icon: ReactNode }> = [
  { id: "auto", name: "自动", desc: "有 Key 走云端，否则本地", icon: <Sparkles size={14} /> },
  { id: "local", name: "本地 Whisper", desc: "离线 faster-whisper", icon: <Cpu size={14} /> },
  { id: "siliconflow", name: "云端 SenseVoice", desc: "硅基流动 · 多语种", icon: <Cloud size={14} /> },
];

const PROVIDER_LABELS: Record<string, string> = {
  auto: "自动",
  local: "本地 Whisper",
  siliconflow: "硅基流动 SenseVoice",
};

/** 后端 verified.message 常是英文技术错误；这里给出中文主文案 + 原文详情。 */
function friendlyVerifyMessage(message: string): { text: string; detail?: string } {
  const raw = (message || "").trim();
  const lower = raw.toLowerCase();
  if (/401|unauthor|authentication|invalid.?api.?key|incorrect api key/.test(lower)) {
    return { text: "API Key 无效或未授权（401），请检查密钥是否正确", detail: raw };
  }
  if (/timeout|timed out|超时/.test(lower)) {
    return { text: "连接超时：无法在限定时间内访问该地址", detail: raw };
  }
  if (/429|rate.?limit|too many requests/.test(lower)) {
    return { text: "请求太频繁（429），请稍后再试", detail: raw };
  }
  if (/404|not.?found|model.*(not|does not).*(exist|available)/.test(lower)) {
    return { text: "接口或模型不存在（404），请检查 Base URL / 模型名", detail: raw };
  }
  return { text: raw || "连接失败，请检查 Base URL / Key / 模型名" };
}

function backendLabel(backend: string): string {
  if (!backend) return "—";
  if (backend.includes("sensevoice")) return "SenseVoice 云端";
  if (backend.includes("faster")) return "本地 Whisper";
  return backend;
}

function sourceLabel(source: string): string {
  if (source === "file") return "来源：界面保存";
  if (source === "env") return "来源：.env";
  return source ? `来源：${source}` : "";
}

function Pill({ tone, children }: { tone: "ok" | "warn" | "off" | "err"; children: ReactNode }) {
  return <span className={`se-pill ${tone}`}>{children}</span>;
}

function FeedbackBar({ feedback }: { feedback: Feedback }) {
  const Icon = feedback.tone === "ok" ? CheckCircle2 : feedback.tone === "info" ? Zap : AlertCircle;
  return (
    <div className={`se-feedback ${feedback.tone}`} role="status">
      <Icon size={13} className="mt-0.5 shrink-0" />
      <div className="min-w-0 flex-1">
        <p>{feedback.text}</p>
        {feedback.detail ? <p className="se-feedback-detail">{feedback.detail}</p> : null}
      </div>
    </div>
  );
}

export function SettingsPanel({ onClose, onChanged }: SettingsPanelProps) {
  const { showToast } = useToast();

  /* ------------------------------- LLM ------------------------------- */
  const [llmLoading, setLlmLoading] = useState(true);
  const [llmError, setLlmError] = useState<string | null>(null);
  const [llmConfig, setLlmConfig] = useState<LlmConfigView | null>(null);
  const [llmHealth, setLlmHealth] = useState<LlmHealthView | null>(null);
  const [llmPresets, setLlmPresets] = useState<LlmPreset[]>([]);
  const [llmPresetId, setLlmPresetId] = useState("");
  const [llmBaseUrl, setLlmBaseUrl] = useState("");
  const [llmModel, setLlmModel] = useState("");
  const [llmKey, setLlmKey] = useState("");
  const [llmBusy, setLlmBusy] = useState<null | "test" | "save" | "reset">(null);
  const [llmFeedback, setLlmFeedback] = useState<Feedback | null>(null);

  /* ------------------------------- STT ------------------------------- */
  const [sttLoading, setSttLoading] = useState(true);
  const [sttError, setSttError] = useState<string | null>(null);
  const [sttUnavailable, setSttUnavailable] = useState(false);
  const [sttConfig, setSttConfig] = useState<SttConfigView | null>(null);
  const [sttHealth, setSttHealth] = useState<SttHealthView | null>(null);
  const [sttPresets, setSttPresets] = useState<SttPreset[]>(DEFAULT_STT_PRESETS);
  const [provider, setProvider] = useState<SttProvider>("auto");
  const [sttBaseUrl, setSttBaseUrl] = useState(DEFAULT_STT_CONFIG.base_url);
  const [sttModel, setSttModel] = useState(DEFAULT_STT_CONFIG.model);
  const [sttKey, setSttKey] = useState("");
  const [sttBusy, setSttBusy] = useState<null | "test" | "save" | "reset" | "clear">(null);
  const [sttFeedback, setSttFeedback] = useState<Feedback | null>(null);

  /* ------------------------------ 数据加载 ------------------------------ */

  const applyLlmConfig = useCallback((config: LlmConfigView | null) => {
    setLlmConfig(config);
    if (!config) return;
    setLlmBaseUrl(config.base_url);
    setLlmModel(config.model);
    setLlmKey("");
  }, []);

  const loadLlm = useCallback(
    async (silent = false) => {
      if (!silent) {
        setLlmLoading(true);
        setLlmError(null);
      }
      try {
        const payload = await getLlmSettings();
        setLlmConfig(payload.config);
        setLlmHealth(payload.health);
        setLlmPresets(payload.presets);
        if (!silent && payload.config) {
          setLlmBaseUrl(payload.config.base_url);
          setLlmModel(payload.config.model);
          setLlmKey("");
        }
        if (!silent) setLlmError(null);
      } catch (err) {
        if (!silent) setLlmError(apiErrorText(err));
      } finally {
        if (!silent) setLlmLoading(false);
      }
    },
    [],
  );

  const applySttPayload = useCallback(
    (config: SttConfigView | null, health: SttHealthView | null, presets: SttPreset[]) => {
      const effective = config ?? DEFAULT_STT_CONFIG;
      setSttConfig(config);
      setSttHealth(health);
      setSttPresets((prev) => (presets.length > 0 ? presets : prev.length > 0 ? prev : DEFAULT_STT_PRESETS));
      setProvider(effective.provider);
      setSttBaseUrl(effective.base_url || DEFAULT_STT_CONFIG.base_url);
      setSttModel(effective.model || DEFAULT_STT_CONFIG.model);
      setSttKey("");
    },
    [],
  );

  const loadStt = useCallback(
    async (silent = false) => {
      if (!silent) {
        setSttLoading(true);
        setSttError(null);
      }
      try {
        const payload = await getSttSettings();
        setSttUnavailable(false);
        setSttError(null);
        applySttPayload(payload.config, payload.health, payload.presets);
      } catch (err) {
        const notReady = err instanceof ApiError && (err.status === 404 || err.status === 405);
        if (notReady) {
          setSttUnavailable(true);
          applySttPayload(DEFAULT_STT_CONFIG, null, DEFAULT_STT_PRESETS);
        } else if (!silent) {
          setSttError(apiErrorText(err));
        }
      } finally {
        if (!silent) setSttLoading(false);
      }
    },
    [applySttPayload],
  );

  useEffect(() => {
    void loadLlm();
    void loadStt();
  }, [loadLlm, loadStt]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  /* -------------------------------- LLM 交互 -------------------------------- */

  const llmStatus: LlmStatus = !llmHealth
    ? "unknown"
    : llmHealth.error
      ? "err"
      : llmHealth.configured
        ? "ok"
        : llmHealth.mock
          ? "mock"
          : "off";

  const llmStatusPill =
    llmStatus === "ok" ? (
      <Pill tone="ok">已连接真实模型</Pill>
    ) : llmStatus === "mock" ? (
      <Pill tone="warn">演示模式（无 Key）</Pill>
    ) : llmStatus === "err" ? (
      <Pill tone="err">连接异常</Pill>
    ) : (
      <Pill tone="off">未配置</Pill>
    );

  const applyLlmPreset = (id: string) => {
    setLlmPresetId(id);
    const preset = llmPresets.find((item) => item.id === id);
    if (!preset) return;
    if (preset.base_url) setLlmBaseUrl(preset.base_url);
    if (preset.model) setLlmModel(preset.model);
    setLlmFeedback({
      tone: "info",
      text: `已填入「${preset.name}」的端点与模型`,
      detail: preset.note || "填入 API Key 后点「保存并启用」即可生效。",
    });
  };

  const buildLlmInput = (): LlmSaveInput => {
    // 保存立即落盘并生效；网络验证交给「测试连接」按钮，避免保存被云端阻塞
    const input: LlmSaveInput = { verify: false };
    if (llmBaseUrl.trim()) input.base_url = llmBaseUrl.trim();
    if (llmModel.trim()) input.model = llmModel.trim();
    if (llmKey.trim()) input.api_key = llmKey.trim();
    return input;
  };

  const handleLlmTest = async () => {
    if (llmBusy) return;
    setLlmBusy("test");
    setLlmFeedback(null);
    try {
      const result = await testLlmSettings({
        base_url: llmBaseUrl.trim() || undefined,
        model: llmModel.trim() || undefined,
        api_key: llmKey.trim() || undefined,
      });
      if (result.ok) {
        setLlmFeedback({
          tone: "ok",
          text: result.message || "连接成功",
          detail: `${Math.round(result.latency_ms)} ms${result.model_reply ? ` · 模型回复：${result.model_reply}` : ""}`,
        });
      } else {
        const friendly = friendlyVerifyMessage(result.message);
        setLlmFeedback({ tone: "err", text: friendly.text, detail: friendly.detail });
      }
    } catch (err) {
      setLlmFeedback({ tone: "err", text: apiErrorText(err) });
    } finally {
      setLlmBusy(null);
    }
  };

  const handleLlmSave = async () => {
    if (llmBusy) return;
    setLlmBusy("save");
    setLlmFeedback(null);
    try {
      const result = await saveLlmSettings(buildLlmInput());
      if (result.config) applyLlmConfig(result.config);
      const verified = result.verified;
      if (result.ok) {
        setLlmFeedback({
          tone: "ok",
          text: result.message || "已保存并生效",
          detail: verified
            ? `${Math.round(verified.latency_ms)} ms${verified.model_reply ? ` · 模型回复：${verified.model_reply}` : ""}`
            : "已即时生效（未做网络验证）；可点「测试连接」验证连通性，云端可能较慢。",
        });
        showToast("LLM 配置已保存并生效", "success");
      } else {
        const friendly = friendlyVerifyMessage(verified?.message || result.message);
        setLlmFeedback({
          tone: "err",
          text: `配置已保存，但连接测试失败：${friendly.text}`,
          detail: friendly.detail,
        });
      }
      onChanged?.();
      void loadLlm(true);
    } catch (err) {
      setLlmFeedback({ tone: "err", text: apiErrorText(err) });
      showToast("LLM 配置保存失败", "error");
    } finally {
      setLlmBusy(null);
    }
  };

  const handleLlmReset = async () => {
    if (llmBusy) return;
    if (!window.confirm("恢复为 .env 默认 LLM 配置？界面保存的配置会被删除。")) return;
    setLlmBusy("reset");
    setLlmFeedback(null);
    try {
      const result = await resetLlmSettings();
      showToast(result.message || "已恢复 .env 默认配置", "success");
      setLlmFeedback({ tone: "ok", text: result.message || "已恢复为 .env 默认配置" });
      setLlmPresetId("");
      await loadLlm(true);
      onChanged?.();
    } catch (err) {
      setLlmFeedback({ tone: "err", text: apiErrorText(err) });
    } finally {
      setLlmBusy(null);
    }
  };

  /* -------------------------------- STT 交互 -------------------------------- */

  const sttActiveBackend = sttHealth?.backend ?? "—";
  const sttConfigured = sttHealth?.configured ?? sttConfig?.configured ?? false;
  const sttProviderText =
    provider === "siliconflow" ? "硅基流动 SenseVoiceSmall" : provider === "local" ? "本地 Whisper" : "自动";

  const changeProvider = (next: SttProvider) => {
    setProvider(next);
    setSttFeedback(null);
    if (next === "siliconflow") {
      const preset = sttPresets.find((item) => item.id === "siliconflow");
      setSttBaseUrl(preset?.base_url || "https://api.siliconflow.cn/v1");
      setSttModel(preset?.model || "FunAudioLLM/SenseVoiceSmall");
    } else if (next === "local") {
      const preset = sttPresets.find((item) => item.id === "local");
      if (preset?.model) setSttModel(preset.model);
    }
  };

  const sttKeyPlaceholder = sttConfig?.api_key_set
    ? `已设置：${sttConfig.api_key_masked}`
    : provider === "local"
      ? "本地模式无需密钥"
      : "未设置（留空 = 不修改）";

  const buildSttInput = (verify: boolean) => {
    const input: {
      provider: SttProvider;
      base_url?: string;
      model?: string;
      api_key?: string;
      language?: string;
      timeout_s?: number;
      verify?: boolean;
    } = {
      provider,
      model: sttModel.trim() || undefined,
      language: sttConfig?.language || "en",
      timeout_s: sttConfig?.timeout_s || 30,
      verify,
    };
    if (sttBaseUrl.trim()) input.base_url = sttBaseUrl.trim();
    if (sttKey.trim()) input.api_key = sttKey.trim();
    return input;
  };

  const handleSttTest = async () => {
    if (sttBusy) return;
    setSttBusy("test");
    setSttFeedback(null);
    try {
      const result = await testSttSettings(buildSttInput(false));
      if (result.ok) {
        setSttFeedback({
          tone: "ok",
          text: result.message || "连接成功",
          detail: `${Math.round(result.latency_ms)} ms${result.text ? ` · 识别结果：${result.text}` : " · 已用 0.5s 提示音验证"}`,
        });
      } else if (provider === "local") {
        setSttFeedback({
          tone: "info",
          text: "本地 Whisper 使用内置模型，无需云端连接测试",
          detail: "保存后立即以本地模式生效；云端测试需要选择「云端 SenseVoice」并填写 API Key。",
        });
      } else {
        const friendly = friendlyVerifyMessage(result.message);
        setSttFeedback({ tone: "err", text: friendly.text, detail: friendly.detail });
      }
    } catch (err) {
      setSttFeedback({ tone: "err", text: apiErrorText(err) });
    } finally {
      setSttBusy(null);
    }
  };

  const handleSttSave = async () => {
    if (sttBusy) return;
    setSttBusy("save");
    setSttFeedback(null);
    try {
      // 立即保存并热生效；真实网络验证交给「测试连接」（SiliconFlow 免费模型可能排队很久）
      const result = await saveSttSettings(buildSttInput(false));
      applySttPayload(result.config, result.health, result.presets);
      const verified = result.verified;
      if (verified && !verified.ok) {
        const friendly = friendlyVerifyMessage(verified.message);
        setSttFeedback({
          tone: "err",
          text: `配置已保存，但连接验证失败：${friendly.text}`,
          detail: friendly.detail ?? "运行时会自动回退本地 Whisper，可修改后重试。",
        });
        showToast("STT 配置已保存（验证未通过）", "error");
      } else {
        setSttFeedback({
          tone: "ok",
          text: "STT 配置已保存并热生效（即时）",
          detail: verified
            ? `${Math.round(verified.latency_ms)} ms${verified.text ? ` · 识别结果：${verified.text}` : ""}`
            : "未做网络验证；可点「测试连接」验证 SiliconFlow，免费模型可能排队（较慢）。",
        });
        showToast("STT 配置已保存并生效", "success");
      }
      onChanged?.();
      void loadStt(true);
    } catch (err) {
      setSttFeedback({ tone: "err", text: apiErrorText(err) });
      showToast("STT 配置保存失败", "error");
    } finally {
      setSttBusy(null);
    }
  };

  const handleSttReset = async () => {
    if (sttBusy) return;
    if (!window.confirm("恢复 STT 默认配置？（会删除 data/stt_config.json，回到 .env / 内置默认）")) return;
    setSttBusy("reset");
    setSttFeedback(null);
    try {
      const payload = await resetSttSettings();
      applySttPayload(payload.config, payload.health, payload.presets);
      setSttFeedback({ tone: "ok", text: "已恢复默认 STT 配置" });
      showToast("已恢复默认 STT 配置", "success");
      onChanged?.();
      void loadStt(true);
    } catch (err) {
      setSttFeedback({ tone: "err", text: apiErrorText(err) });
    } finally {
      setSttBusy(null);
    }
  };

  const handleSttClearKey = async () => {
    if (sttBusy || !sttConfig?.api_key_set) return;
    if (!window.confirm("清除已保存的 STT API Key？")) return;
    setSttBusy("clear");
    setSttFeedback(null);
    try {
      const result = await saveSttSettings({
        provider,
        api_key: "__CLEAR__",
        verify: false,
        language: sttConfig?.language || "en",
        timeout_s: sttConfig?.timeout_s || 30,
      });
      applySttPayload(result.config, result.health, result.presets);
      setSttFeedback({ tone: "info", text: "已清除 STT API Key" });
      showToast("STT 密钥已清除", "success");
      onChanged?.();
    } catch (err) {
      setSttFeedback({ tone: "err", text: apiErrorText(err) });
    } finally {
      setSttBusy(null);
    }
  };

  /* -------------------------------- 渲染 -------------------------------- */

  const sttFieldsDisabled = provider === "local" || sttUnavailable;

  return (
    <div className="se-settings">
      <header className="se-settings-header">
        <button type="button" onClick={onClose} className="se-chat-back" aria-label="返回">
          <ChevronLeft size={24} />
        </button>
        <div className="min-w-0 flex-1 text-center">
          <span className="text-[15px] font-semibold">个人设置</span>
        </div>
        <span className="w-[32px] shrink-0 text-right text-[color:var(--se-faint)]" aria-hidden>
          <Settings2 size={17} />
        </span>
      </header>

      <div className="se-settings-body">

          <TTSSettingsSection />
        {/* 概览 */}
        <div className="se-settings-summary">
          <div className="se-settings-summary-item">
            <span className="se-settings-summary-label">对话模型</span>
            <span className="se-settings-summary-value">{llmConfig?.model || "—"}</span>
          </div>
          <div className="se-settings-summary-divider" aria-hidden />
          <div className="se-settings-summary-item">
            <span className="se-settings-summary-label">语音识别</span>
            <span className="se-settings-summary-value">{backendLabel(sttActiveBackend)}</span>
          </div>
        </div>

        {/* ============================ LLM ============================ */}
        <section className="se-setting-section">
          <div className="se-setting-head">
            <span className="se-setting-icon">
              <Server size={15} />
            </span>
            <div className="min-w-0 flex-1">
              <p className="se-setting-title">对话模型 LLM</p>
              <p className="se-setting-sub">
                用于生成好友回复与润色建议
                {llmConfig?.source ? ` · ${sourceLabel(llmConfig.source)}` : ""}
              </p>
            </div>
            {llmStatusPill}
          </div>

          {llmLoading ? <Spinner label="读取 LLM 配置…" /> : null}
          {llmError ? <ErrorBanner message={`读取 LLM 配置失败：${llmError}`} onRetry={() => void loadLlm()} /> : null}

          {!llmLoading && !llmError ? (
            <>
              <label className="se-field">
                <span>预设供应商</span>
                <select
                  className="se-select"
                  value={llmPresetId}
                  onChange={(event) => applyLlmPreset(event.target.value)}
                  aria-label="LLM 预设供应商"
                >
                  <option value="">自定义（不套用预设）</option>
                  {llmPresets.map((preset) => (
                    <option key={preset.id} value={preset.id}>
                      {preset.name}
                    </option>
                  ))}
                </select>
              </label>

              <label className="se-field">
                <span>Base URL</span>
                <input
                  value={llmBaseUrl}
                  onChange={(event) => setLlmBaseUrl(event.target.value)}
                  placeholder="https://api.deepseek.com/v1"
                  autoComplete="off"
                  spellCheck={false}
                />
              </label>

              <label className="se-field">
                <span>API Key</span>
                <input
                  type="password"
                  value={llmKey}
                  onChange={(event) => setLlmKey(event.target.value)}
                  placeholder={llmConfig?.api_key_set ? `已设置：${llmConfig.api_key_masked}（留空不修改）` : "sk-..."}
                  autoComplete="new-password"
                  spellCheck={false}
                />
              </label>

              <label className="se-field">
                <span>模型</span>
                <input
                  value={llmModel}
                  onChange={(event) => setLlmModel(event.target.value)}
                  placeholder="deepseek-chat"
                  autoComplete="off"
                  spellCheck={false}
                />
              </label>

              {llmFeedback ? <FeedbackBar feedback={llmFeedback} /> : null}

              <div className="se-setting-actions">
                <button
                  type="button"
                  className="se-btn ghost"
                  onClick={() => void handleLlmTest()}
                  disabled={llmBusy !== null}
                >
                  {llmBusy === "test" ? <Loader2 size={14} className="animate-spin" /> : <Zap size={14} />}
                  {llmBusy === "test" ? "测试中…" : "测试连接"}
                </button>
                <button
                  type="button"
                  className="se-btn primary"
                  onClick={() => void handleLlmSave()}
                  disabled={llmBusy !== null}
                >
                  {llmBusy === "save" ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
                  {llmBusy === "save" ? "保存中…" : "保存并启用"}
                </button>
              </div>

              <button
                type="button"
                className="se-btn-link"
                onClick={() => void handleLlmReset()}
                disabled={llmBusy !== null}
              >
                {llmBusy === "reset" ? <Loader2 size={12} className="animate-spin" /> : <RotateCcw size={12} />}
                恢复 .env 默认配置
              </button>

              <p className="se-setting-footnote">
                <KeyRound size={11} />
                密钥只保存在本机 data/llm_config.json，接口永不回传明文；保存即时生效，
                「测试连接」会真实请求模型服务。
              </p>
            </>
          ) : null}
        </section>

        {/* ============================ STT ============================ */}
        <section className="se-setting-section">
          <div className="se-setting-head">
            <span className="se-setting-icon">
              <Mic size={15} />
            </span>
            <div className="min-w-0 flex-1">
              <p className="se-setting-title">语音识别 STT</p>
              <p className="se-setting-sub">
                语音条转写 · 当前生效：
                <b className={sttConfigured ? "text-[color:var(--se-green-ink)]" : ""}>
                  {backendLabel(sttActiveBackend)}
                </b>
                {sttConfig?.configured ? "（云端已配置）" : ""}
              </p>
            </div>
            {sttUnavailable ? (
              <Pill tone="off">接口未就绪</Pill>
            ) : sttActiveBackend.includes("sensevoice") ? (
              <Pill tone="ok">云端</Pill>
            ) : (
              <Pill tone="warn">本地</Pill>
            )}
          </div>

          {sttUnavailable ? (
            <div className="se-notice">
              <AlertCircle size={13} className="mt-0.5 shrink-0" />
              <div className="min-w-0 flex-1">
                <p>后端 STT 设置接口尚未就绪（404）。以下为默认值，保存/测试暂不可用。</p>
                <p className="se-notice-dim">接口就绪后点「重新检测」即可加载真实配置。</p>
              </div>
              <button type="button" className="se-notice-retry" onClick={() => void loadStt()}>
                <RefreshCw size={12} />
                重新检测
              </button>
            </div>
          ) : null}

          {sttLoading ? <Spinner label="读取 STT 配置…" /> : null}
          {sttError ? <ErrorBanner message={`读取 STT 配置失败：${sttError}`} onRetry={() => void loadStt()} /> : null}

          {!sttLoading && !sttError ? (
            <>
              <div className="se-choice-group" role="radiogroup" aria-label="STT provider">
                {STT_PROVIDERS.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    role="radio"
                    aria-checked={provider === item.id}
                    className={`se-choice ${provider === item.id ? "active" : ""}`}
                    onClick={() => changeProvider(item.id)}
                    disabled={sttUnavailable}
                  >
                    <span className="se-choice-name">
                      {item.icon}
                      {item.name}
                    </span>
                    <span className="se-choice-desc">{item.desc}</span>
                  </button>
                ))}
              </div>

              <p className="se-provider-hint">
                {provider === "auto"
                  ? "自动：填了 Key 就走云端 SenseVoiceSmall，没 Key 时用本地 faster-whisper。"
                  : provider === "local"
                    ? "本地模式：只用 faster-whisper，云端字段不生效、密钥不参与。"
                    : "云端模式：优先 SiliconFlow SenseVoiceSmall，失败自动回退本地，不会中断聊天。"}
              </p>

              <label className="se-field">
                <span>Base URL</span>
                <input
                  value={sttBaseUrl}
                  onChange={(event) => setSttBaseUrl(event.target.value)}
                  placeholder="https://api.siliconflow.cn/v1"
                  disabled={sttFieldsDisabled}
                  autoComplete="off"
                  spellCheck={false}
                />
              </label>

              <label className="se-field">
                <span>API Key</span>
                <input
                  type="password"
                  value={sttKey}
                  onChange={(event) => setSttKey(event.target.value)}
                  placeholder={sttKeyPlaceholder}
                  disabled={sttFieldsDisabled}
                  autoComplete="new-password"
                  spellCheck={false}
                />
              </label>

              <label className="se-field">
                <span>模型</span>
                <input
                  value={sttModel}
                  onChange={(event) => setSttModel(event.target.value)}
                  placeholder="FunAudioLLM/SenseVoiceSmall"
                  disabled={sttFieldsDisabled}
                  autoComplete="off"
                  spellCheck={false}
                />
              </label>

              {sttFeedback ? <FeedbackBar feedback={sttFeedback} /> : null}

              <div className="se-setting-actions">
                <button
                  type="button"
                  className="se-btn ghost"
                  onClick={() => void handleSttTest()}
                  disabled={sttBusy !== null || sttUnavailable}
                >
                  {sttBusy === "test" ? <Loader2 size={14} className="animate-spin" /> : <Zap size={14} />}
                  {sttBusy === "test" ? "测试中…（可能较慢）" : "测试连接"}
                </button>
                <button
                  type="button"
                  className="se-btn primary"
                  onClick={() => void handleSttSave()}
                  disabled={sttBusy !== null || sttUnavailable}
                >
                  {sttBusy === "save" ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
                  {sttBusy === "save" ? "保存中…" : "保存"}
                </button>
              </div>

              <div className="se-setting-subactions">
                <button
                  type="button"
                  className="se-btn-link"
                  onClick={() => void handleSttReset()}
                  disabled={sttBusy !== null || sttUnavailable}
                >
                  {sttBusy === "reset" ? <Loader2 size={12} className="animate-spin" /> : <RotateCcw size={12} />}
                  恢复默认
                </button>
                {sttConfig?.api_key_set ? (
                  <button
                    type="button"
                    className="se-btn-link danger"
                    onClick={() => void handleSttClearKey()}
                    disabled={sttBusy !== null || sttUnavailable}
                  >
                    {sttBusy === "clear" ? (
                      <Loader2 size={12} className="animate-spin" />
                    ) : (
                      <Trash2 size={12} />
                    )}
                    清除密钥
                  </button>
                ) : null}
              </div>

              {/* health */}
              <div className="se-health">
                <div className="se-health-row">
                  <span>生效后端</span>
                  <b>{sttActiveBackend}</b>
                </div>
                <div className="se-health-row">
                  <span>Provider</span>
                  <b>{PROVIDER_LABELS[sttHealth?.provider ?? provider] ?? (sttHealth?.provider ?? provider)}</b>
                </div>
                <div className="se-health-row">
                  <span>模型</span>
                  <b className="truncate">{sttHealth?.model || sttModel || "—"}</b>
                </div>
                <div className="se-health-row">
                  <span>云端已配置</span>
                  <b>{sttConfigured ? "是" : "否"}</b>
                </div>
                {sttHealth && sttHealth.requests > 0 ? (
                  <div className="se-health-row">
                    <span>请求数 / 最近成功</span>
                    <b>
                      {sttHealth.requests}
                      {sttHealth.last_success_at ? ` · ${relativeLabel(sttHealth.last_success_at)}` : ""}
                    </b>
                  </div>
                ) : null}
                {sttHealth?.last_error ? (
                  <div className="se-health-row error">
                    <span>最近错误</span>
                    <b className="se-health-error">{sttHealth.last_error}</b>
                  </div>
                ) : null}
              </div>

              <p className="se-setting-footnote">
                <KeyRound size={11} />
                密钥只写入本机 data/stt_config.json（0600），云端失败会自动回退本地；保存即时生效，
                「测试连接」会真实请求 SiliconFlow（免费模型可能排队，最长约 30 秒）。
              </p>
            </>
          ) : null}
        </section>

        <p className="se-settings-note">
          保存后立即生效，无需重启服务。
          <br />
          LLM 用于生成回复；STT 决定语音条转写走本地还是 SiliconFlow 云端。
        </p>
      </div>
    </div>
  );
}
