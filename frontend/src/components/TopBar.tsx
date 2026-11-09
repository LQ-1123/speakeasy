"use client";

import { Activity, AudioLines, Cpu, Gauge, Plus, Power, RefreshCw, Settings2, Volume2, Zap } from "lucide-react";

import type { ConnectionStatus, EnginesInfo, TurnMetrics } from "@/lib/types";

interface TopBarProps {
  status: ConnectionStatus;
  statusDetail: string;
  engines: EnginesInfo;
  metrics: TurnMetrics;
  websocketOpen: boolean;
  onConnect: () => void;
  onDisconnect: () => void;
  firstAudioMs: number | undefined;
  /** 打开 LLM 设置弹窗 */
  onOpenSettings: () => void;
  /** 打开声音克隆弹窗 */
  onOpenVoiceClone: () => void;
  /** 打开创建角色 */
  onOpenCreatePersona: () => void;
}

const STATUS_META: Record<
  ConnectionStatus,
  { label: string; color: string; ledClass: string; dot: string }
> = {
  disconnected: {
    label: "未连接",
    color: "var(--text-faint)",
    ledClass: "",
    dot: "var(--text-faint)",
  },
  connecting: {
    label: "连接中",
    color: "var(--accent-warn)",
    ledClass: "led-connecting",
    dot: "var(--accent-warn)",
  },
  ready: {
    label: "就绪",
    color: "var(--accent-ok)",
    ledClass: "",
    dot: "var(--accent-ok)",
  },
  error: {
    label: "未连接后端",
    color: "var(--accent-danger)",
    ledClass: "",
    dot: "var(--accent-danger)",
  },
};

/** first_audio_ms 的 SLO 分档：<1000 优、<2000 可、否则告警 */
function firstAudioClass(ms: number | undefined): string {
  if (ms === undefined) return "text-[color:var(--text-faint)]";
  if (ms < 1000) return "text-[color:var(--accent-ok)]";
  if (ms < 2000) return "text-[color:var(--accent)]";
  return "text-[color:var(--accent-danger)]";
}

function fmtMs(ms: number | undefined): string {
  if (ms === undefined) return "—";
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`;
  return `${Math.round(ms)}ms`;
}

export function TopBar({
  status,
  statusDetail,
  engines,
  metrics,
  websocketOpen,
  onConnect,
  onDisconnect,
  firstAudioMs,
  onOpenSettings,
  onOpenVoiceClone,
  onOpenCreatePersona,
}: TopBarProps) {
  const meta = STATUS_META[status];

  return (
    <header className="flex h-14 shrink-0 items-center gap-4 border-b border-[color:var(--line)] bg-[color:var(--surface)] px-4">
      {/* 品牌 */}
      <div className="flex items-center gap-2.5">
        <div className="flex h-7 w-7 items-center justify-center rounded-sm border border-[color:var(--accent-line)] bg-[color:var(--accent-dim)]">
          <AudioLines size={15} className="text-[color:var(--accent)]" strokeWidth={2.2} />
        </div>
        <div className="leading-none">
          <div className="text-[13px] font-semibold tracking-tight text-[color:var(--text)]">
            SpeakEasy
          </div>
          <div className="mt-1 text-[9.5px] font-medium uppercase tracking-[0.18em] text-[color:var(--text-faint)]">
            Spoken English
          </div>
        </div>
      </div>

      <div className="h-6 w-px bg-[color:var(--line)]" />

      {/* 连接状态灯 */}
      <div className="flex min-w-0 items-center gap-2">
        <span
          className={`h-2 w-2 shrink-0 rounded-full ${meta.ledClass}`}
          style={{ background: meta.dot, boxShadow: `0 0 8px ${meta.dot}` }}
          aria-hidden
        />
        <div className="min-w-0">
          <div className="text-[11.5px] font-medium" style={{ color: meta.color }}>
            {meta.label}
          </div>
          <div className="max-w-[260px] truncate text-[10px] text-[color:var(--text-faint)]" title={statusDetail}>
            {statusDetail}
          </div>
        </div>
      </div>

      {/* 引擎信息 */}
      <div className="hidden items-center gap-3 lg:flex">
        <EngineChip icon={<Cpu size={11} />} label="STT" value={engines.stt ?? "未知"} />
        <EngineChip icon={<Activity size={11} />} label="TTS" value={engines.tts ?? "未知"} />
        <EngineChip
          icon={<Zap size={11} />}
          label="LLM"
          value={
            engines.llmConfigured === false
              ? "未配置"
              : (engines.llmModel ?? (engines.llmConfigured ? "已配置" : "未知"))
          }
          warn={engines.llmConfigured === false}
        />
      </div>

      <div className="ml-auto flex items-center gap-3">
        {/* first_audio_ms 指标徽章 */}
        <div
          className="flex items-center gap-2 rounded-sm border border-[color:var(--line-strong)] bg-[color:var(--surface-2)] px-2.5 py-1"
          title="first_audio_ms：从识别完成到第一帧音频推送的时间（目标 < 1000ms）"
        >
          <Gauge size={12} className="text-[color:var(--text-faint)]" />
          <span className="panel-label">FIRST AUDIO</span>
          <span className={`tnum text-[12.5px] font-semibold ${firstAudioClass(firstAudioMs)}`}>
            {fmtMs(firstAudioMs)}
          </span>
        </div>

        {metrics.total_ms !== undefined && (
          <div className="hidden items-center gap-1.5 xl:flex" title="上一回合总耗时">
            <span className="panel-label">TOTAL</span>
            <span className="tnum text-[11px] text-[color:var(--text-dim)]">{fmtMs(metrics.total_ms)}</span>
          </div>
        )}

        {/* 创建角色 */}
        <button
          type="button"
          onClick={onOpenCreatePersona}
          title="设计你自己的角色和场景"
          className="flex items-center gap-1.5 rounded-sm border border-[color:var(--line-strong)] bg-[color:var(--surface-2)] px-2.5 py-1.5 text-[11px] font-medium text-[color:var(--text-dim)] transition-colors hover:border-[color:var(--accent-line)] hover:text-[color:var(--accent)]"
        >
          <Plus size={11} />
          <span className="hidden md:inline">造角色</span>
        </button>

        {/* 声音克隆：上传录音用你的音色说话 */}
        <button
          type="button"
          onClick={onOpenVoiceClone}
          title="上传 30s–1min 录音，让角色用你的音色说话"
          className="flex items-center gap-1.5 rounded-sm border border-[color:var(--line-strong)] bg-[color:var(--surface-2)] px-2.5 py-1.5 text-[11px] font-medium text-[color:var(--text-dim)] transition-colors hover:border-[color:var(--accent-line)] hover:text-[color:var(--accent)]"
        >
          <Volume2 size={11} />
          <span className="hidden sm:inline">克隆音色</span>
          <Volume2 size={11} className="sm:hidden" />
        </button>

        {/* 模型设置：未配置时高亮 + 呼吸动画，引导用户去填 Key */}
        <button
          type="button"
          onClick={onOpenSettings}
          title={
            engines.llmConfigured === false
              ? "尚未配置文本模型 —— 点击填写 Base URL 与 API Key"
              : "模型设置（Base URL / API Key / 模型名）"
          }
          className={`flex items-center gap-1.5 rounded-sm border px-2.5 py-1.5 text-[11px] font-medium transition-colors ${
            engines.llmConfigured === false
              ? "border-[color:var(--accent-warn)] bg-[color:var(--accent-warn)]/15 text-[color:var(--accent-warn)] led-connecting hover:bg-[color:var(--accent-warn)]/25"
              : "border-[color:var(--line-strong)] bg-[color:var(--surface-2)] text-[color:var(--text-dim)] hover:border-[color:var(--accent-line)] hover:text-[color:var(--accent)]"
          }`}
        >
          <Settings2 size={11} />
          模型设置
          {engines.llmConfigured === false && (
            <span className="ml-0.5 h-1.5 w-1.5 rounded-full bg-[color:var(--accent-warn)]" />
          )}
        </button>

        {/* 连接控制 */}
        <button
          type="button"
          onClick={websocketOpen ? onDisconnect : onConnect}
          className={`flex items-center gap-1.5 rounded-sm border px-2.5 py-1.5 text-[11px] font-medium transition-colors ${
            websocketOpen
              ? "border-[color:var(--line-strong)] bg-[color:var(--surface-2)] text-[color:var(--text-dim)] hover:border-[color:var(--accent-danger)] hover:text-[color:var(--accent-danger)]"
              : "border-[color:var(--accent-line)] bg-[color:var(--accent-dim)] text-[color:var(--accent)] hover:bg-[color:var(--accent)]/20"
          }`}
        >
          {websocketOpen ? (
            <>
              <Power size={11} />
              断开
            </>
          ) : (
            <>
              <RefreshCw size={11} />
              连接后端
            </>
          )}
        </button>
      </div>
    </header>
  );
}

function EngineChip({
  icon,
  label,
  value,
  warn,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  warn?: boolean;
}) {
  return (
    <div className="flex items-center gap-1.5" title={`${label}: ${value}`}>
      <span className="text-[color:var(--text-faint)]">{icon}</span>
      <span className="panel-label">{label}</span>
      <span
        className={`tnum max-w-[150px] truncate text-[10.5px] ${
          warn ? "text-[color:var(--accent-danger)]" : "text-[color:var(--text-dim)]"
        }`}
      >
        {value}
      </span>
    </div>
  );
}
