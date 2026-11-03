"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Hand, Mic, Send, Settings2, Square, Sparkles, X } from "lucide-react";

import { listMicrophones } from "@/lib/audio/mic";

interface ComposerProps {
  recording: boolean;
  connected: boolean;
  playing: boolean;
  micAvailable: boolean;
  micError: string | null;
  /** 当前选中的输入设备（空 = 系统默认） */
  micDeviceId?: string;
  onSelectMicDevice?: (deviceId: string) => void;
  onToggleRecording: () => void;
  onSend: (text: string) => void;
  onInterrupt: () => void;
  onRequestSuggestion: () => void;
}

/**
 * 底部输入栏：录音 + 文字输入并存。
 *
 * 无麦克风时文字输入框**必须始终可用**（协议 1.4 `text` 消息），
 * 这样任何环境（CI、没有麦克风的桌面、权限被拒）都能完整演示。
 */
export function Composer({
  recording,
  connected,
  playing,
  micAvailable,
  micError,
  micDeviceId = "",
  onSelectMicDevice,
  onToggleRecording,
  onSend,
  onInterrupt,
  onRequestSuggestion,
}: ComposerProps) {
  const [draft, setDraft] = useState("");
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  /* ------------------------------------------------- 麦克风设备选择 -------- */
  const [devOpen, setDevOpen] = useState(false);
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [devLoading, setDevLoading] = useState(false);
  const [testLevel, setTestLevel] = useState(0);
  const [testing, setTesting] = useState(false);
  const testRef = useRef<{ ctx: AudioContext; stream: MediaStream } | null>(null);
  const testRaf = useRef<number>(0);

  const refreshDevices = useCallback(async () => {
    setDevLoading(true);
    const list = await listMicrophones();
    setDevices(list);
    setDevLoading(false);
  }, []);

  /** 实时电平测试：录 1 秒左右，看这根麦克风有没有声音 */
  const runLevelTest = useCallback(async (deviceId: string) => {
    // 清理上一次
    if (testRef.current) {
      cancelAnimationFrame(testRaf.current);
      testRef.current.stream.getTracks().forEach((t) => t.stop());
      void testRef.current.ctx.close();
      testRef.current = null;
    }
    setTesting(true);
    setTestLevel(0);
    try {
      const constraints: MediaStreamConstraints = {
        audio: deviceId ? { deviceId: { exact: deviceId } } : true,
      };
      const stream = await navigator.mediaDevices.getUserMedia(constraints);
      const ctx = new AudioContext();
      const src = ctx.createMediaStreamSource(stream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 1024;
      src.connect(analyser);
      const buf = new Float32Array(analyser.fftSize);
      testRef.current = { ctx, stream };

      let peak = 0;
      const loop = () => {
        analyser.getFloatTimeDomainData(buf);
        let m = 0;
        for (let i = 0; i < buf.length; i++) m = Math.max(m, Math.abs(buf[i]));
        if (m > peak) peak = m;
        setTestLevel(peak);
        testRaf.current = requestAnimationFrame(loop);
      };
      loop();
    } catch {
      setTestLevel(-1);
    } finally {
      setTesting(false);
    }
  }, []);

  const stopLevelTest = useCallback(() => {
    cancelAnimationFrame(testRaf.current);
    if (testRef.current) {
      testRef.current.stream.getTracks().forEach((t) => t.stop());
      void testRef.current.ctx.close();
      testRef.current = null;
    }
    setTestLevel(0);
  }, []);

  // 打开面板时刷新设备；关闭时清理测试
  useEffect(() => {
    if (devOpen) {
      void refreshDevices();
      return;
    }
    stopLevelTest();
  }, [devOpen, refreshDevices, stopLevelTest]);

  // 组件卸载时清理
  useEffect(() => () => stopLevelTest(), [stopLevelTest]);

  const hasVirtual = devices.some((d) =>
    /cable|virtual|steam|oculus|vb-audio|nvidia|obs/i.test(d.label),
  );

  // textarea 自适应高度（最多 4 行）
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "0px";
    const next = Math.min(96, Math.max(34, el.scrollHeight));
    el.style.height = `${next}px`;
  }, [draft]);

  const submit = () => {
    const text = draft.trim();
    if (!text) return;
    onSend(text);
    setDraft("");
    inputRef.current?.focus();
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  };

  return (
    <div className="shrink-0 border-t border-[color:var(--line-strong)] bg-[color:var(--surface)] px-4 py-3">
      <div className="mx-auto flex max-w-[820px] items-end gap-2.5">
        {/* 录音按钮 */}
        <button
          type="button"
          onClick={onToggleRecording}
          aria-pressed={recording}
          title={recording ? "结束本句（发送 audio_end）" : "开始录音（上行 16kHz PCM16）"}
          className={`relative flex h-10 w-10 shrink-0 items-center justify-center rounded-sm border transition-all ${
            recording
              ? "rec-pulse border-[color:var(--accent-danger)] bg-[color:var(--accent-danger)] text-[#0b0d12]"
              : "border-[color:var(--line-strong)] bg-[color:var(--surface-2)] text-[color:var(--text-dim)] hover:border-[color:var(--accent-line)] hover:text-[color:var(--accent)]"
          }`}
        >
          {recording ? <Square size={15} strokeWidth={3} /> : <Mic size={16} />}
        </button>

        {/* 输入框 */}
        <div className="relative min-w-0 flex-1">
          <textarea
            ref={inputRef}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={handleKeyDown}
            rows={1}
            placeholder={
              connected
                ? "打字也行（Enter 发送，Shift+Enter 换行）— 走 text 消息，跳过 STT"
                : "后端未连接，仍可输入；连接后会发送"
            }
            className="scroll-thin block w-full resize-none rounded-sm border border-[color:var(--line-strong)] bg-[color:var(--surface-2)] px-3 py-2 text-[13px] leading-relaxed text-[color:var(--text)] placeholder:text-[color:var(--text-faint)] focus:border-[color:var(--accent-line)] focus:outline-none"
            style={{ minHeight: 34 }}
          />
        </div>

        {/* 发请求润色 */}
        <button
          type="button"
          onClick={onRequestSuggestion}
          disabled={!connected}
          title="对最近一句请求润色建议"
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-sm border border-[color:var(--line-strong)] bg-[color:var(--surface-2)] text-[color:var(--text-dim)] transition-colors hover:border-[color:var(--accent-line)] hover:text-[color:var(--accent)] disabled:opacity-40 disabled:hover:border-[color:var(--line-strong)] disabled:hover:text-[color:var(--text-dim)]"
        >
          <Sparkles size={15} />
        </button>

        {/* 打断 */}
        {playing && (
          <button
            type="button"
            onClick={onInterrupt}
            title="打断当前朗读（barge-in）"
            className="flex h-10 shrink-0 items-center gap-1.5 rounded-sm border border-[color:var(--accent-danger)]/60 bg-[color:var(--accent-danger)]/12 px-2.5 text-[11px] font-medium text-[color:var(--accent-danger)] transition-colors hover:bg-[color:var(--accent-danger)]/20"
          >
            <Hand size={13} />
            打断
          </button>
        )}

        {/* 发送 */}
        <button
          type="button"
          onClick={submit}
          disabled={draft.trim().length === 0}
          title="发送文本（Enter）"
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-sm border border-[color:var(--accent-line)] bg-[color:var(--accent-dim)] text-[color:var(--accent)] transition-colors hover:bg-[color:var(--accent)]/25 disabled:border-[color:var(--line-strong)] disabled:bg-transparent disabled:text-[color:var(--text-faint)]"
        >
          <Send size={15} />
        </button>
      </div>

      {/* 麦克风问题诊断条 —— 不阻塞文字输入 */}
      {!micAvailable && micError && (
        <div className="mx-auto mt-2 flex max-w-[820px] items-start gap-2 text-[10px] leading-relaxed text-[color:var(--accent-warn)]">
          <span className="min-w-0 flex-1">
            麦克风不可用：{micError} — 文字输入完全可用，功能不受影响。
          </span>
          {onSelectMicDevice && (
            <button
              type="button"
              onClick={() => setDevOpen((v) => !v)}
              className="flex shrink-0 items-center gap-1 rounded-[2px] border border-[color:var(--accent-warn)] px-1.5 py-0.5 text-[9.5px] transition-colors hover:bg-[color:var(--accent-warn)]/10"
            >
              <Settings2 size={9} />
              选择麦克风
            </button>
          )}
        </div>
      )}

      {/* 麦克风选择面板 */}
      {devOpen && (
        <div className="mx-auto mt-2 max-w-[820px] rounded-[2px] border border-[color:var(--line-strong)] bg-[color:var(--surface-2)] p-2.5">
          <div className="mb-2 flex items-center gap-2">
            <Mic size={11} className="text-[color:var(--accent)]" />
            <span className="flex-1 text-[10.5px] font-medium text-[color:var(--text)]">
              选择输入设备
            </span>
            <button
              type="button"
              onClick={() => void refreshDevices()}
              disabled={devLoading}
              className="text-[9.5px] text-[color:var(--text-faint)] hover:text-[color:var(--accent)] disabled:opacity-40"
            >
              {devLoading ? "扫描中…" : "重新扫描"}
            </button>
            <button
              type="button"
              onClick={() => setDevOpen(false)}
              className="text-[color:var(--text-faint)] hover:text-[color:var(--text)]"
            >
              <X size={11} />
            </button>
          </div>

          {hasVirtual && (
            <p className="mb-2 rounded-[2px] border border-[color:var(--accent-warn)]/40 bg-[color:var(--accent-warn)]/5 px-2 py-1 text-[9px] leading-relaxed text-[color:var(--accent-warn)]">
              检测到虚拟声卡（VB-Cable / Steam / Oculus / OBS）。这些设备能打开但**收不到真人说话**。
              请选一个带「麦克风 / Microphone / Headset」字样的真实设备。
            </p>
          )}

          <div className="space-y-1">
            {/* 系统默认 */}
            <button
              type="button"
              onClick={() => {
                onSelectMicDevice?.("");
                setDevOpen(false);
              }}
              className={`flex w-full items-center gap-2 rounded-[2px] border px-2 py-1.5 text-left text-[10.5px] transition-colors ${
                micDeviceId === ""
                  ? "border-[color:var(--accent-line)] bg-[color:var(--accent)]/10 text-[color:var(--accent)]"
                  : "border-transparent text-[color:var(--text-dim)] hover:bg-[color:var(--surface)]"
              }`}
            >
              <span className="min-w-0 flex-1">系统默认设备</span>
              <span className="shrink-0 text-[9px] text-[color:var(--text-faint)]">不推荐</span>
            </button>

            {devices.map((d) => {
              const isVirtual = /cable|virtual|steam|oculus|vb-audio|nvidia|obs/i.test(d.label);
              const active = micDeviceId === d.deviceId;
              return (
                <div
                  key={d.deviceId}
                  className={`flex items-center gap-2 rounded-[2px] border px-2 py-1.5 transition-colors ${
                    active
                      ? "border-[color:var(--accent-line)] bg-[color:var(--accent)]/10"
                      : "border-transparent hover:bg-[color:var(--surface)]"
                  }`}
                >
                  <button
                    type="button"
                    onClick={() => {
                      onSelectMicDevice?.(d.deviceId);
                      setDevOpen(false);
                    }}
                    className="min-w-0 flex-1 text-left"
                  >
                    <span
                      className={`block truncate text-[10.5px] ${
                        active ? "text-[color:var(--accent)]" : "text-[color:var(--text-dim)]"
                      }`}
                    >
                      {d.label || "(无标签 — 请先允许麦克风权限)"}
                    </span>
                    {isVirtual && (
                      <span className="text-[8.5px] text-[color:var(--accent-warn)]">
                        虚拟声卡，收不到人声
                      </span>
                    )}
                  </button>
                  {active && <span className="shrink-0 text-[9px] text-[color:var(--accent)]">使用中</span>}
                  <button
                    type="button"
                    onClick={() => void runLevelTest(d.deviceId)}
                    title="测试这根麦克风有没有声音（说句话看电平条）"
                    className="shrink-0 rounded-[2px] border border-[color:var(--line-strong)] px-1.5 py-0.5 text-[9px] text-[color:var(--text-faint)] hover:text-[color:var(--accent)]"
                  >
                    测电平
                  </button>
                </div>
              );
            })}

            {devices.length === 0 && !devLoading && (
              <p className="px-2 py-2 text-[10px] text-[color:var(--text-faint)]">
                没有检测到输入设备。请先允许麦克风权限再点「重新扫描」。
              </p>
            )}
          </div>

          {/* 电平条 */}
          {(testLevel !== 0 || testing) && (
            <div className="mt-2">
              <div className="mb-1 flex items-center justify-between text-[9px] text-[color:var(--text-faint)]">
                <span>{testing ? "正在打开麦克风…" : "峰值电平（说话时应有明显跳动）"}</span>
                <span>
                  {testLevel < 0 ? "打不开" : testLevel === 0 ? "静音" : testLevel.toFixed(3)}
                </span>
              </div>
              <div className="h-1.5 overflow-hidden rounded-full bg-[color:var(--surface)]">
                <div
                  className={`h-full transition-[width] duration-75 ${
                    testLevel > 0.01 ? "bg-[color:var(--accent-ok)]" : "bg-[color:var(--accent-warn)]"
                  }`}
                  style={{ width: `${Math.min(100, Math.max(0, (testLevel || 0) * 300))}%` }}
                />
              </div>
              <p className="mt-1 text-[9px] leading-relaxed text-[color:var(--text-faint)]">
                {testLevel > 0.01
                  ? "✓ 这根麦克风能收到声音"
                  : testLevel === 0
                    ? "静音 — 说句话试试；如果一直没反应，换一根设备"
                    : "打不开 — 换一根设备"}
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
