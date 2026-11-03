"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, Info, Settings2, X } from "lucide-react";

import { Composer } from "@/components/Composer";
import { ConversationView } from "@/components/ConversationView";
import { LlmSettingsDialog } from "@/components/LlmSettingsDialog";
import { PersonaPanel } from "@/components/PersonaPanel";
import { SuggestionPanel } from "@/components/SuggestionPanel";
import { TopBar } from "@/components/TopBar";
import { VoiceCloneDialog } from "@/components/VoiceCloneDialog";
import { AccentMarketDialog } from "@/components/AccentMarketDialog";
import { CreatePersonaDialog } from "@/components/CreatePersonaDialog";
import { Waveform, type WaveMode } from "@/components/Waveform";
import { fetchHealth, fetchPersonas } from "@/lib/api";
import { FALLBACK_MODES, FALLBACK_PERSONAS } from "@/lib/fallback";
import { useConversation } from "@/lib/useConversation";
import type { Notice, PersonaPublic } from "@/lib/types";

/** 内置兜底：初次渲染即用，避免任何"空列表"闪烁 */
const BOOT_PERSONAS: PersonaPublic[] = FALLBACK_PERSONAS;

export default function Page() {
  const conversation = useConversation(BOOT_PERSONAS, FALLBACK_MODES);
  const [mobilePanel, setMobilePanel] = useState<"none" | "personas" | "suggestions">("none");
  const [apiPersonas, setApiPersonas] = useState<PersonaPublic[] | null>(null);
  const [apiError, setApiError] = useState<string | null>(null);
  const [firstAudioMs, setFirstAudioMs] = useState<number | undefined>(undefined);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsBannerDismissed, setSettingsBannerDismissed] = useState(false);
  const [voiceOpen, setVoiceOpen] = useState(false);
  const [marketOpen, setMarketOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);

  const {
    status,
    statusDetail,
    engines,
    personas,
    modes,
    activePersonaId,
    activeMode,
    messages,
    suggestions,
    notices,
    metrics,
    recording,
    micAvailable,
    micError,
    micDeviceId,
    setMicDeviceId,
    playerStats,
    activeSentence,
    vadSpeaking,
    pending,
    websocketOpen,
    connect,
    disconnect,
    sendText,
    selectPersona,
    selectMode,
    interrupt,
    toggleRecording,
    requestSuggestion,
    dismissNotice,
    mic,
    player,
  } = conversation;

  /* ---------------------------------------------------------- /api/personas */

  // 角色列表：初次加载 + 自定义角色/口音变更后可手动刷新
  const reloadPersonas = useCallback(async () => {
    const payload = await fetchPersonas();
    if (payload.ok && payload.personas.length > 0) {
      setApiPersonas(payload.personas);
      setApiError(null);
    } else {
      setApiError(payload.error ?? "接口不可用");
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      await reloadPersonas();
      if (cancelled) return;
    })();
    return () => {
      cancelled = true;
    };
  }, [reloadPersonas]);

  // 后端引擎信息在未连接 WS 时的补充来源（best effort，失败静默）。
  // 仅做一次探测：让"后端在线但 WS 未连"时顶栏也能尽早有意义，
  // 真正的引擎信息以 ready 消息为准。
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      await fetchHealth();
      if (cancelled) return;
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  /* ---------------------------------------------------------- 指标同步 */

  useEffect(() => {
    if (metrics.first_audio_ms !== undefined) setFirstAudioMs(metrics.first_audio_ms);
  }, [metrics.first_audio_ms]);

  /* ---------------------------------------------------------- 角色列表合并 */

  // 优先级：WS ready 下发的 personas > /api/personas > 内置 fallback
  const displayPersonas = personas.length > 0 ? personas : (apiPersonas ?? BOOT_PERSONAS);

  const activePersona = useMemo(
    () => displayPersonas.find((p) => p.id === activePersonaId) ?? displayPersonas[0],
    [displayPersonas, activePersonaId],
  );

  /* ---------------------------------------------------------- 波形取样回调 */

  // 上行：麦克风 AnalyserNode；下行：播放 AnalyserNode。
  // 用 useCallback 稳定引用，避免 Waveform 的 rAF 循环反复重建。
  const micSample = useCallback(
    (target: Uint8Array<ArrayBuffer>): boolean => {
      if (!mic || !recording) return false;
      return mic.readWaveform(target);
    },
    [mic, recording],
  );

  const playerSample = useCallback(
    (target: Uint8Array<ArrayBuffer>): boolean => {
      if (!player) return false;
      return player.readWaveform(target);
    },
    [player],
  );

  const micMode: WaveMode = recording ? "live" : "idle";
  const playMode: WaveMode = playerStats.playing ? "playing" : "idle";

  const gapWarning =
    playerStats.gapFrames > 0 ? `丢帧 ${playerStats.gapFrames}` : null;

  /* ---------------------------------------------------------- 移动端抽屉 */

  const closeDrawer = () => setMobilePanel("none");

  return (
    <main className="flex h-screen flex-col overflow-hidden bg-[color:var(--bg)]">
      <TopBar
        status={status}
        statusDetail={statusDetail}
        engines={engines}
        metrics={metrics}
        websocketOpen={websocketOpen}
        onConnect={connect}
        onDisconnect={disconnect}
        firstAudioMs={firstAudioMs}
        onOpenSettings={() => setSettingsOpen(true)}
        onOpenVoiceClone={() => setVoiceOpen(true)}
        onOpenAccentMarket={() => setMarketOpen(true)}
        onOpenCreatePersona={() => setCreateOpen(true)}
      />

      {/* ------------------------------------- 未配置模型提示条 */}
      {engines.llmConfigured === false && !settingsBannerDismissed && (
        <div className="flex shrink-0 items-center gap-2 border-b border-[color:var(--accent-warn)]/40 bg-[color:var(--accent-warn)]/10 px-4 py-1.5">
          <AlertTriangle size={11} className="shrink-0 text-[color:var(--accent-warn)]" />
          <p className="min-w-0 flex-1 text-[10.5px] leading-relaxed text-[color:var(--accent-warn)]">
            尚未配置文本模型：当前回复由本地规则引擎生成（内容是演示性的）。
          </p>
          <button
            type="button"
            onClick={() => setSettingsOpen(true)}
            className="shrink-0 rounded-[2px] border border-[color:var(--accent-warn)] px-2 py-0.5 text-[10px] font-medium text-[color:var(--accent-warn)] transition-colors hover:bg-[color:var(--accent-warn)]/20"
          >
            填写 Base URL 与 API Key
          </button>
          <button
            type="button"
            onClick={() => setSettingsBannerDismissed(true)}
            aria-label="关闭提示"
            className="shrink-0 text-[color:var(--accent-warn)]/70 transition-colors hover:text-[color:var(--accent-warn)]"
          >
            <X size={11} />
          </button>
        </div>
      )}

      {/* --------------------------------------------------- 通知条 */}
      <NoticeBar notices={notices} onDismiss={dismissNotice} />

      {/* --------------------------------------------------- 三栏主体 */}
      <div className="flex min-h-0 flex-1 overflow-hidden">
        {/* 左栏：角色 + 模式（≥1280 常驻） */}
        <div className="hidden w-[248px] shrink-0 border-r border-[color:var(--line)] xl:block">
          <PersonaPanel
            personas={displayPersonas}
            modes={modes}
            activePersonaId={activePersonaId}
            activeMode={activeMode}
            onSelectPersona={selectPersona}
            onSelectMode={selectMode}
            onOpenVoiceClone={() => setVoiceOpen(true)}
          />
        </div>

        {/* 中栏：对话 + 波形 + 输入 */}
        <section className="flex min-w-0 flex-1 flex-col">
          <div className="flex shrink-0 items-center gap-3 border-b border-[color:var(--line)] px-4 py-2">
            {/* 移动端打开角色面板 */}
            <button
              type="button"
              onClick={() => setMobilePanel("personas")}
              className="flex items-center gap-1.5 rounded-[2px] border border-[color:var(--line-strong)] px-2 py-1 text-[10.5px] text-[color:var(--text-dim)] transition-colors hover:border-[color:var(--accent-line)] hover:text-[color:var(--accent)] xl:hidden"
            >
              <Settings2 size={11} />
              {activePersona?.flag} {activePersona?.name ?? "角色"}
            </button>

            {/* 当前场景一句话 */}
            <p className="hidden min-w-0 flex-1 truncate text-[10.5px] leading-relaxed text-[color:var(--text-faint)] md:block">
              {activePersona?.scene ?? "选一个角色开始对话"}
            </p>

            <div className="ml-auto flex items-center gap-2">
              {apiError && (
                <span
                  className="tnum hidden text-[9.5px] text-[color:var(--accent-warn)] sm:inline"
                  title={`/api/personas 请求失败：${apiError}，当前显示内置角色`}
                >
                  内置角色
                </span>
              )}
              <button
                type="button"
                onClick={() => setMobilePanel("suggestions")}
                className="flex items-center gap-1.5 rounded-[2px] border border-[color:var(--line-strong)] px-2 py-1 text-[10.5px] text-[color:var(--text-dim)] transition-colors hover:border-[color:var(--accent-line)] hover:text-[color:var(--accent)] xl:hidden"
              >
                润色
                {suggestions.length > 0 && (
                  <span className="tnum rounded-[2px] bg-[color:var(--accent-dim)] px-1 text-[9px] text-[color:var(--accent)]">
                    {suggestions.length}
                  </span>
                )}
              </button>
            </div>
          </div>

          <ConversationView
            messages={messages}
            persona={activePersona}
            activeSentence={activeSentence}
            vadSpeaking={vadSpeaking}
            pending={pending}
            connected={status === "ready"}
          />

          {/* 双波形：上行（琥珀）/ 下行（薄荷） */}
          <div className="shrink-0 border-t border-[color:var(--line)] bg-[color:var(--surface)] px-4 pt-2.5">
            <div className="mx-auto grid max-w-[820px] grid-cols-1 gap-2 sm:grid-cols-2">
              <Waveform
                sample={micSample}
                mode={micMode}
                tone="accent"
                height={44}
                label="上行 · MIC 16kHz"
              />
              <Waveform
                sample={playerSample}
                mode={playMode}
                tone="mint"
                height={44}
                label="下行 · TTS 24kHz"
                warning={gapWarning}
              />
            </div>
          </div>

          <Composer
            recording={recording}
            connected={status === "ready"}
            playing={playerStats.playing}
            micAvailable={micAvailable}
            micError={micError}
            micDeviceId={micDeviceId}
            onSelectMicDevice={setMicDeviceId}
            onToggleRecording={toggleRecording}
            onSend={sendText}
            onInterrupt={interrupt}
            onRequestSuggestion={() => requestSuggestion()}
          />
        </section>

        {/* 右栏：润色建议（≥1280 常驻） */}
        <div className="hidden w-[288px] shrink-0 border-l border-[color:var(--line)] xl:block">
          <SuggestionPanel
            suggestions={suggestions}
            connected={status === "ready"}
            onRequest={() => requestSuggestion()}
          />
        </div>
      </div>

      {/* --------------------------------------------------- 移动端抽屉 */}
      {mobilePanel !== "none" && (
        <div className="fixed inset-0 z-40 xl:hidden" role="dialog" aria-modal="true">
          <button
            type="button"
            aria-label="关闭面板"
            onClick={closeDrawer}
            className="absolute inset-0 bg-black/60"
          />
          <div
            className={`absolute bottom-0 left-0 right-0 h-[68vh] overflow-hidden rounded-t-md border-t border-[color:var(--line-strong)] bg-[color:var(--surface)] shadow-2xl ${
              mobilePanel === "personas" ? "anim-in" : "anim-in"
            }`}
          >
            <div className="flex items-center justify-between border-b border-[color:var(--line)] px-3.5 py-2">
              <span className="panel-label">
                {mobilePanel === "personas" ? "角色与模式" : "润色建议"}
              </span>
              <button
                type="button"
                onClick={closeDrawer}
                className="flex h-6 w-6 items-center justify-center rounded-[2px] text-[color:var(--text-faint)] hover:text-[color:var(--text)]"
              >
                <X size={13} />
              </button>
            </div>
            <div className="h-[calc(68vh-40px)]">
              {mobilePanel === "personas" ? (
                <PersonaPanel
                  personas={displayPersonas}
                  modes={modes}
                  activePersonaId={activePersonaId}
                  activeMode={activeMode}
                  onSelectPersona={(id) => {
                    selectPersona(id);
                    closeDrawer();
                  }}
                  onSelectMode={selectMode}
                  onOpenVoiceClone={() => {
                    closeDrawer();
                    setVoiceOpen(true);
                  }}
                />
              ) : (
                <SuggestionPanel
                  suggestions={suggestions}
                  connected={status === "ready"}
                  onRequest={() => requestSuggestion()}
                />
              )}
            </div>
          </div>
        </div>
      )}
      {/* --------------------------------------------------- LLM 设置弹窗 */}
      <LlmSettingsDialog
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        onSaved={() => {
          setSettingsBannerDismissed(false);
          void fetchHealth();
        }}
      />

      {/* --------------------------------------------------- 声音克隆弹窗 */}
      <VoiceCloneDialog
        open={voiceOpen}
        onClose={() => setVoiceOpen(false)}
        personaId={activePersonaId}
        personaName={activePersona?.name ?? "对方"}
      />

      {/* --------------------------------------------------- 口音市场 */}
      <AccentMarketDialog
        open={marketOpen}
        onClose={() => setMarketOpen(false)}
        personaId={activePersonaId}
        personaName={activePersona?.name ?? "对方"}
        onApplied={() => {
          void fetchHealth();
          void reloadPersonas();
        }}
      />

      {/* --------------------------------------------------- 创建角色 */}
      <CreatePersonaDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onSaved={() => {
          void fetchHealth();
          void reloadPersonas();
        }}
      />
    </main>
  );
}

/* ------------------------------------------------------------------ 通知条 */

function NoticeBar({ notices, onDismiss }: { notices: Notice[]; onDismiss: (id: string) => void }) {
  if (notices.length === 0) return null;

  return (
    <div className="shrink-0 border-b border-[color:var(--line)] bg-[color:var(--surface-2)]">
      <div className="flex flex-col divide-y divide-[color:var(--line)]">
        {notices.map((notice) => {
          const tone =
            notice.level === "error"
              ? "text-[color:var(--accent-danger)]"
              : notice.level === "warn"
                ? "text-[color:var(--accent-warn)]"
                : "text-[color:var(--text-dim)]";
          const Icon = notice.level === "info" ? Info : AlertTriangle;
          return (
            <div key={notice.id} className="flex items-start gap-2 px-4 py-1.5">
              <Icon size={11} className={`mt-0.5 shrink-0 ${tone}`} />
              <p className={`min-w-0 flex-1 text-[10.5px] leading-relaxed ${tone}`}>{notice.text}</p>
              <button
                type="button"
                onClick={() => onDismiss(notice.id)}
                aria-label="关闭提示"
                className="shrink-0 text-[color:var(--text-faint)] transition-colors hover:text-[color:var(--text)]"
              >
                <X size={11} />
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}
