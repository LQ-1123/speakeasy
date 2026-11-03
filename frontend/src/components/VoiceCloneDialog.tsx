"use client";

/**
 * 声音克隆面板：上传 30s–1min 录音 → 用你的音色说话。
 *
 * 交互流程：
 * 1. 用户上传音频文件（或直接在页面录音）
 * 2. 前端转成 wav 后 POST /api/voice/upload（走 ffmpeg 转码在后端做）
 * 3. 后端规范化（去静音/重采样/归一化）并登记音色
 * 4. 用户点「试听」调 /api/voice/preview/{id} 听效果
 * 5. 点「应用到角色」调 /api/voice/assign，之后该角色用这个音色
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  Check,
  Loader2,
  Mic,
  Play,
  Square,
  Trash2,
  Upload,
  Volume2,
  X,
} from "lucide-react";

interface VoiceItem {
  voice_id: string;
  duration_s: number;
  created_at: number;
}

interface VoiceStatus {
  available: boolean;
  backend: string;
  /** 口音能否保留（级联架构）。true = 换音色同时保留真实口音 */
  accent_preserving?: boolean;
  min_duration_s: number;
  max_duration_s: number;
  recommended: string;
  voices: VoiceItem[];
  assignments: Record<string, string>;
  personas?: Array<{ persona_id: string; accent: string; edge_voice: string }>;
  note: string;
}

interface VoiceCloneDialogProps {
  open: boolean;
  onClose: () => void;
  personaId: string;
  personaName: string;
}

type Phase = "idle" | "uploading" | "ready" | "error";

const MAX_RECORD_S = 60;

export function VoiceCloneDialog({
  open,
  onClose,
  personaId,
  personaName,
}: VoiceCloneDialogProps) {
  const [status, setStatus] = useState<VoiceStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [phase, setPhase] = useState<Phase>("idle");
  const [message, setMessage] = useState("");
  const [detail, setDetail] = useState("");
  const [voiceId, setVoiceId] = useState("");
  const [busy, setBusy] = useState(false);
  const [recording, setRecording] = useState(false);
  const [recordSec, setRecordSec] = useState(0);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);

  const fileRef = useRef<HTMLInputElement | null>(null);
  const mediaRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetch("/api/voice/status", { cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j = (await r.json()) as VoiceStatus;
      setStatus(j);
      if (!j.available) {
        setPhase("error");
        setMessage("XTTS 声音克隆不可用");
        setDetail("后端未安装 coqui-tts。请查看 docs/INSTALL_LOG.md");
      }
    } catch (e) {
      setPhase("error");
      setMessage("无法读取声音克隆状态");
      setDetail(e instanceof Error ? e.message : String(e));
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  useEffect(() => {
    if (!open) {
      setPhase("idle");
      setMessage("");
      setDetail("");
      setVoiceId("");
      setPreviewUrl(null);
      stopRecording();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const stopRecording = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    const mr = mediaRef.current;
    if (mr && mr.state !== "inactive") {
      try {
        mr.stop();
      } catch {
        /* noop */
      }
    }
    mediaRef.current = null;
    setRecording(false);
  }, []);

  /* ------------------------------ 上传 ------------------------------ */

  const doUpload = async (blob: Blob, filename: string, format: string) => {
    setPhase("uploading");
    setMessage(`正在上传并分析音频（${(blob.size / 1024).toFixed(0)}KB）…`);
    setDetail("");
    setBusy(true);

    try {
      const fd = new FormData();
      fd.append("file", new File([blob], filename, { type: blob.type || "audio/wav" }));
      fd.append("voice_id", `v_${Date.now().toString(36)}`);
      fd.append("ref_text", "");

      const r = await fetch("/api/voice/upload", { method: "POST", body: fd });
      const j = (await r.json()) as Record<string, unknown>;
      if (!r.ok) {
        throw new Error(
          typeof j.detail === "string" ? j.detail : `HTTP ${r.status}`,
        );
      }
      const vid = String(j.voice_id ?? "");
      setVoiceId(vid);
      setPhase("ready");
      setMessage(`音色已就绪：${Number(j.duration_s ?? 0).toFixed(1)} 秒`);
      setDetail(
        Boolean(j.trimmed)
          ? "录音较长，已自动截取中间最清晰的一段。"
          : "可以点「试听」确认效果，或直接应用到角色。",
      );
      await load();
    } catch (e) {
      setPhase("error");
      setMessage("上传失败");
      setDetail(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      void format;
    }
  };

  const onPickFile = (file: File) => {
    if (!file) return;
    const fmt = (file.name.split(".").pop() ?? "wav").toLowerCase();
    const allowed = ["wav", "mp3", "m4a", "flac", "ogg", "webm", "aac", "opus"];
    if (!allowed.includes(fmt)) {
      setPhase("error");
      setMessage("不支持的音频格式");
      setDetail(`支持：${allowed.join(" / ")}`);
      return;
    }
    if (file.size > 25 * 1024 * 1024) {
      setPhase("error");
      setMessage("文件太大");
      setDetail(`上限 25MB，当前 ${(file.size / 1024 / 1024).toFixed(1)}MB`);
      return;
    }
    void doUpload(file, file.name, fmt);
  };

  /* ------------------------------ 录音 ------------------------------ */

  const startRecording = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
        },
      });
      const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
        ? "audio/webm;codecs=opus"
        : "audio/webm";
      const mr = new MediaRecorder(stream, { mimeType: mime });
      chunksRef.current = [];
      mr.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      mr.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        const blob = new Blob(chunksRef.current, { type: mime });
        if (blob.size > 1000) void doUpload(blob, "recording.webm", "webm");
      };
      mr.start(200);
      mediaRef.current = mr;
      setRecording(true);
      setRecordSec(0);
      setPhase("idle");
      setMessage("正在录音…请朗读一段英文（20–30 秒最佳）");
      setDetail("推荐内容：自我介绍、描述你的一天、讲一件最近发生的事。");
      timerRef.current = setInterval(() => {
        setRecordSec((s) => {
          const next = s + 1;
          if (next >= MAX_RECORD_S) stopRecording();
          return next;
        });
      }, 1000);
    } catch (e) {
      setPhase("error");
      setMessage("无法访问麦克风");
      setDetail(e instanceof Error ? e.message : String(e));
    }
  };

  /* ------------------------------ 试听 ------------------------------ */

  const doPreview = async (vid: string) => {
    setBusy(true);
    setMessage("正在合成试听…");
    setDetail("");
    try {
      // 带上 persona_id：试听会走级联，用该角色的真实口音
      const r = await fetch(
        `/api/voice/preview/${encodeURIComponent(vid)}?persona_id=${encodeURIComponent(personaId)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            text: "Right then, that's a flat white for you. Cheers, guv.",
          }),
        },
      );
      if (!r.ok) {
        const j = (await r.json().catch(() => ({}))) as Record<string, unknown>;
        throw new Error(
          typeof j.detail === "string" ? j.detail : `HTTP ${r.status}`,
        );
      }
      const blob = await r.blob();
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      const url = URL.createObjectURL(blob);
      setPreviewUrl(url);
      setMessage("试听已生成，正在播放…");
      setDetail("如果听起来像你，就点「应用到角色」。");
      requestAnimationFrame(() => {
        if (audioRef.current) {
          audioRef.current.src = url;
          void audioRef.current.play().catch(() => undefined);
        }
      });
    } catch (e) {
      setPhase("error");
      setMessage("试听失败");
      setDetail(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  /* ------------------------------ 应用 ------------------------------ */

  const doAssign = async (vid: string, targetPersona: string) => {
    setBusy(true);
    setMessage("正在应用到角色…");
    try {
      const r = await fetch("/api/voice/assign", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ persona_id: targetPersona, voice_id: vid }),
      });
      const j = (await r.json()) as Record<string, unknown>;
      if (!r.ok) {
        throw new Error(typeof j.detail === "string" ? j.detail : `HTTP ${r.status}`);
      }
      setPhase("ready");
      setMessage(String(j.message ?? "已应用"));
      setDetail("返回对话页，对方就会用这个音色说话了。");
      await load();
    } catch (e) {
      setPhase("error");
      setMessage("应用失败");
      setDetail(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const doDelete = async (vid: string) => {
    setBusy(true);
    try {
      const r = await fetch(`/api/voice/${encodeURIComponent(vid)}`, {
        method: "DELETE",
      });
      if (r.ok) {
        setMessage("音色已删除");
        setDetail("");
        if (voiceId === vid) setVoiceId("");
        await load();
      }
    } finally {
      setBusy(false);
    }
  };

  if (!open) return null;

  const minS = status?.min_duration_s ?? 3;
  const maxS = status?.max_duration_s ?? 120;
  const assignedHere = status?.assignments?.[personaId];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true">
      <button
        type="button"
        aria-label="关闭"
        onClick={onClose}
        className="absolute inset-0 bg-black/70 backdrop-blur-[2px]"
      />
      <div className="relative flex max-h-[90vh] w-full max-w-[600px] flex-col overflow-hidden rounded-[3px] border border-[color:var(--line-strong)] bg-[color:var(--surface)] shadow-2xl">
        {/* 头部 */}
        <div className="flex shrink-0 items-center gap-2.5 border-b border-[color:var(--line)] px-4 py-3">
          <Volume2 size={14} className="text-[color:var(--accent)]" />
          <div className="min-w-0 flex-1">
            <h2 className="text-[13px] font-semibold text-[color:var(--text)]">
              用你的声音说话
            </h2>
            <p className="mt-0.5 text-[9.5px] uppercase tracking-[0.14em] text-[color:var(--text-faint)]">
              Voice Cloning · XTTS-v2
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
              检查声音克隆能力…
            </div>
          ) : (
            <>
              {/* 状态 */}
              {message && (
                <div
                  className={`mb-3.5 rounded-[2px] border px-3 py-2 ${
                    phase === "error"
                      ? "border-[color:var(--accent-danger)] text-[color:var(--accent-danger)]"
                      : phase === "ready"
                        ? "border-[color:var(--accent-ok)] text-[color:var(--accent-ok)]"
                        : "border-[color:var(--accent)] text-[color:var(--accent)]"
                  }`}
                >
                  <div className="flex items-start gap-1.5">
                    {phase === "error" ? (
                      <AlertCircle size={11} className="mt-0.5 shrink-0" />
                    ) : phase === "ready" ? (
                      <Check size={11} className="mt-0.5 shrink-0" />
                    ) : (
                      <Loader2 size={11} className="mt-0.5 shrink-0 animate-spin" />
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

              {status?.available && (
                <>
                  {/* 效果说明：口音保留能力 */}
                  <div className="mb-3 rounded-[2px] border border-[color:var(--line)] bg-[color:var(--surface-2)] px-3 py-2">
                    <p className="text-[10px] leading-relaxed text-[color:var(--text-dim)]">
                      录一段 <b className="text-[color:var(--text)]">10–30 秒</b> 的英文（最少 {minS}s，最多{" "}
                      {maxS / 60} 分钟），系统会学习你的音色，之后{" "}
                      <b className="text-[color:var(--accent)]">{personaName}</b> 就用你的声音说话。
                    </p>
                    {status.accent_preserving ? (
                      <p className="mt-1.5 flex items-start gap-1 text-[9.5px] leading-relaxed text-[color:var(--accent-ok)]">
                        <Check size={10} className="mt-0.5 shrink-0" />
                        <span>
                          口音完整保留：{personaName} 的{" "}
                          <b>
                            {status.personas?.find((p) => p.persona_id === personaId)?.accent ??
                              "母语口音"}
                          </b>{" "}
                          来自真人录音，只会替换成你的音色。
                        </span>
                      </p>
                    ) : (
                      <p className="mt-1.5 flex items-start gap-1 text-[9.5px] leading-relaxed text-[color:var(--accent-warn,#d97706)]">
                        <AlertCircle size={10} className="mt-0.5 shrink-0" />
                        <span>OpenVoice 未就绪：音色会克隆，但口音无法保留。</span>
                      </p>
                    )}
                    <p className="mt-1 text-[9.5px] leading-relaxed text-[color:var(--text-faint)]">
                      建议：安静环境、正常语速、内容连贯。录音只存在本机{" "}
                      <code className="rounded bg-[color:var(--surface)] px-1">data/voices/</code>。
                    </p>
                  </div>

                  {/* 上传 / 录音 */}
                  <div className="mb-3 grid grid-cols-2 gap-2">
                    <button
                      type="button"
                      disabled={busy || recording}
                      onClick={() => fileRef.current?.click()}
                      className="flex items-center justify-center gap-1.5 rounded-[2px] border border-[color:var(--line-strong)] bg-[color:var(--surface-2)] px-3 py-2.5 text-[11px] text-[color:var(--text-dim)] transition-colors hover:border-[color:var(--accent-line)] hover:text-[color:var(--accent)] disabled:opacity-40"
                    >
                      <Upload size={12} />
                      上传音频文件
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={recording ? stopRecording : startRecording}
                      className={`flex items-center justify-center gap-1.5 rounded-[2px] border px-3 py-2.5 text-[11px] transition-colors disabled:opacity-40 ${
                        recording
                          ? "border-[color:var(--accent-danger)] bg-[color:var(--accent-danger)]/15 text-[color:var(--accent-danger)]"
                          : "border-[color:var(--line-strong)] bg-[color:var(--surface-2)] text-[color:var(--text-dim)] hover:border-[color:var(--accent-line)] hover:text-[color:var(--accent)]"
                      }`}
                    >
                      {recording ? (
                        <>
                          <Square size={12} />
                          停止（{recordSec}s）
                        </>
                      ) : (
                        <>
                          <Mic size={12} />
                          直接用麦克风录
                        </>
                      )}
                    </button>
                  </div>

                  <input
                    ref={fileRef}
                    type="file"
                    accept=".wav,.mp3,.m4a,.flac,.ogg,.webm,.aac,.opus,audio/*"
                    className="hidden"
                    onChange={(e) => {
                      const f = e.target.files?.[0];
                      if (f) onPickFile(f);
                      e.target.value = "";
                    }}
                  />

                  {/* 刚上传的音色：试听 + 应用 */}
                  {voiceId && (
                    <div className="mb-3 rounded-[2px] border border-[color:var(--accent-line)] bg-[color:var(--accent-dim)] px-3 py-2.5">
                      <div className="mb-2 flex items-center gap-1.5">
                        <Check size={11} className="text-[color:var(--accent-ok)]" />
                        <span className="text-[10.5px] font-medium text-[color:var(--text)]">
                          新音色：{voiceId}
                        </span>
                      </div>
                      <div className="flex gap-2">
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => void doPreview(voiceId)}
                          className="flex items-center gap-1.5 rounded-[2px] border border-[color:var(--line-strong)] bg-[color:var(--surface)] px-2.5 py-1.5 text-[10.5px] text-[color:var(--text-dim)] transition-colors hover:text-[color:var(--accent)] disabled:opacity-40"
                        >
                          <Play size={10} />
                          试听
                        </button>
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => void doAssign(voiceId, personaId)}
                          className="flex items-center gap-1.5 rounded-[2px] border border-[color:var(--accent-line)] bg-[color:var(--accent)]/20 px-2.5 py-1.5 text-[10.5px] font-medium text-[color:var(--accent)] transition-colors hover:bg-[color:var(--accent)]/30 disabled:opacity-40"
                        >
                          <Check size={10} />
                          应用到 {personaName}
                        </button>
                      </div>
                    </div>
                  )}

                  {/* 已保存的音色 */}
                  {(status.voices.length > 0 || assignedHere) && (
                    <div>
                      <p className="mb-1.5 text-[9.5px] font-medium uppercase tracking-[0.12em] text-[color:var(--text-faint)]">
                        已保存的音色（{status.voices.length}）
                      </p>
                      <div className="space-y-1.5">
                        {status.voices.map((v) => {
                          const boundTo = Object.entries(status.assignments).find(
                            ([, vid]) => vid === v.voice_id,
                          )?.[0];
                          return (
                            <div
                              key={v.voice_id}
                              className="flex items-center gap-2 rounded-[2px] border border-[color:var(--line)] bg-[color:var(--surface-2)] px-2.5 py-1.5"
                            >
                              <div className="min-w-0 flex-1">
                                <div className="truncate text-[10.5px] text-[color:var(--text)]">
                                  {v.voice_id}
                                  {boundTo && (
                                    <span className="ml-1.5 text-[9px] text-[color:var(--accent-ok)]">
                                      → {boundTo}
                                    </span>
                                  )}
                                </div>
                                <div className="text-[9px] text-[color:var(--text-faint)]">
                                  {v.duration_s.toFixed(1)}s
                                </div>
                              </div>
                              <button
                                type="button"
                                disabled={busy}
                                onClick={() => void doPreview(v.voice_id)}
                                title="试听"
                                className="shrink-0 text-[color:var(--text-faint)] transition-colors hover:text-[color:var(--accent)] disabled:opacity-40"
                              >
                                <Play size={11} />
                              </button>
                              <button
                                type="button"
                                disabled={busy}
                                onClick={() => void doAssign(v.voice_id, personaId)}
                                title={`应用到 ${personaName}`}
                                className="shrink-0 text-[color:var(--text-faint)] transition-colors hover:text-[color:var(--accent-ok)] disabled:opacity-40"
                              >
                                <Check size={11} />
                              </button>
                              <button
                                type="button"
                                disabled={busy}
                                onClick={() => void doDelete(v.voice_id)}
                                title="删除"
                                className="shrink-0 text-[color:var(--text-faint)] transition-colors hover:text-[color:var(--accent-danger)] disabled:opacity-40"
                              >
                                <Trash2 size={11} />
                              </button>
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  )}

                  {/* 恢复默认 */}
                  {assignedHere && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => void doAssign("", personaId)}
                      className="mt-2.5 w-full rounded-[2px] border border-[color:var(--line)] px-2 py-1.5 text-[10px] text-[color:var(--text-faint)] transition-colors hover:border-[color:var(--accent-danger)] hover:text-[color:var(--accent-danger)] disabled:opacity-40"
                    >
                      恢复 {personaName} 的默认音色
                    </button>
                  )}
                </>
              )}
            </>
          )}
        </div>

        <audio ref={audioRef} className="hidden" />
      </div>
    </div>
  );
}
