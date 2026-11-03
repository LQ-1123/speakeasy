"use client";

/**
 * 按住说话录音器。
 *
 * - `MediaRecorder` + 浏览器最优容器（Chrome opus/webm，Safari mp4）；
 * - 按住开始，上滑超过阈值进入「松手取消」，<400ms 视为误触不发；
 * - 无麦克风 / 权限拒绝 / 浏览器不支持时通过 `onError` 上报，UI 切到文字兜底。
 */

import { useCallback, useEffect, useRef, useState } from "react";

export interface RecordingResult {
  blob: Blob;
  durationMs: number;
  filename: string;
}

interface RecorderOptions {
  /** 松手且有效（>0.4s）时回调；返回 Promise 期间 UI 进入发送中。 */
  onComplete: (result: RecordingResult) => void | Promise<void>;
  onError?: (message: string) => void;
  /** 开始录音失败（无麦/权限）时回调，用于把 UI 切到文字模式。 */
  onUnavailable?: (message: string) => void;
}

const MIN_DURATION_MS = 400;
const CANCEL_DRAG_PX = 60;
const MIME_CANDIDATES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/ogg;codecs=opus",
  "audio/mp4",
];

function pickMime(): string {
  if (typeof MediaRecorder === "undefined" || typeof MediaRecorder.isTypeSupported !== "function") {
    return "";
  }
  for (const candidate of MIME_CANDIDATES) {
    if (MediaRecorder.isTypeSupported(candidate)) return candidate;
  }
  return "";
}

function filenameFor(mime: string): string {
  if (mime.includes("mp4")) return "voice.mp4";
  if (mime.includes("ogg")) return "voice.ogg";
  return "voice.webm";
}

export function useVoiceRecorder({ onComplete, onError, onUnavailable }: RecorderOptions) {
  const [supported, setSupported] = useState(false);
  const [probing, setProbing] = useState(true);
  const [recording, setRecording] = useState(false);
  const [starting, setStarting] = useState(false);
  const [elapsedMs, setElapsedMs] = useState(0);
  const [cancelArmed, setCancelArmed] = useState(false);
  const [sending, setSending] = useState(false);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const startedAtRef = useRef(0);
  const pressedRef = useRef(false);
  const cancelRef = useRef(false);
  const tickRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const mountedRef = useRef(true);

  /** 预检：接口存在 + 至少一个音频输入设备。不触发权限弹窗。 */
  useEffect(() => {
    mountedRef.current = true;
    let cancelled = false;
    void (async () => {
      let ok = false;
      try {
        ok =
          typeof navigator !== "undefined" &&
          typeof MediaRecorder !== "undefined" &&
          !!navigator.mediaDevices?.getUserMedia;
        if (ok && navigator.mediaDevices?.enumerateDevices) {
          try {
            const devices = await navigator.mediaDevices.enumerateDevices();
            const inputs = devices.filter((d) => d.kind === "audioinput");
            // 某些浏览器在授权前也会返回设备（label 为空），只要存在就认为可能可用
            if (devices.length > 0 && inputs.length === 0) ok = false;
          } catch {
            /* 枚举失败不影响尝试 */
          }
        }
      } catch {
        ok = false;
      }
      if (cancelled) return;
      setSupported(ok);
      setProbing(false);
    })();
    return () => {
      cancelled = true;
      mountedRef.current = false;
    };
  }, []);

  const stopTicker = useCallback(() => {
    if (tickRef.current) {
      clearInterval(tickRef.current);
      tickRef.current = null;
    }
  }, []);

  const releaseStream = useCallback(() => {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
  }, []);

  const cleanupRecorder = useCallback(() => {
    stopTicker();
    releaseStream();
    recorderRef.current = null;
  }, [releaseStream, stopTicker]);

  const reset = useCallback(() => {
    cleanupRecorder();
    setRecording(false);
    setStarting(false);
    setElapsedMs(0);
    setCancelArmed(false);
    cancelRef.current = false;
  }, [cleanupRecorder]);

  const start = useCallback(async (): Promise<boolean> => {
    if (recorderRef.current || starting || sending) return false;
    pressedRef.current = true;
    cancelRef.current = false;
    setCancelArmed(false);
    setStarting(true);

    if (typeof navigator === "undefined" || !navigator.mediaDevices?.getUserMedia) {
      setStarting(false);
      setSupported(false);
      onUnavailable?.("当前环境不支持麦克风，已切换为文字输入");
      return false;
    }

    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (err) {
      setStarting(false);
      setSupported(false);
      const name = err instanceof DOMException ? err.name : "";
      const message =
        name === "NotAllowedError"
          ? "麦克风权限被拒绝，已切换为文字输入"
          : name === "NotFoundError" || name === "DevicesNotFoundError"
            ? "没有检测到麦克风，已切换为文字输入"
            : "无法使用麦克风，已切换为文字输入";
      onUnavailable?.(message);
      return false;
    }

    if (!pressedRef.current) {
      stream.getTracks().forEach((track) => track.stop());
      setStarting(false);
      return false;
    }

    streamRef.current = stream;

    const mime = pickMime();
    let recorder: MediaRecorder;
    try {
      recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    } catch {
      releaseStream();
      setStarting(false);
      setSupported(false);
      onUnavailable?.("当前浏览器不支持录音，已切换为文字输入");
      return false;
    }

    chunksRef.current = [];
    recorder.ondataavailable = (event: BlobEvent) => {
      if (event.data && event.data.size > 0) chunksRef.current.push(event.data);
    };
    recorder.onerror = () => {
      onError?.("录音失败，请重试");
      reset();
    };

    recorderRef.current = recorder;
    startedAtRef.current = performance.now();

    try {
      recorder.start(120);
    } catch {
      cleanupRecorder();
      setStarting(false);
      setSupported(false);
      onUnavailable?.("录音启动失败，已切换为文字输入");
      return false;
    }

    if (!mountedRef.current) return false;
    setStarting(false);
    setRecording(true);
    setElapsedMs(0);
    tickRef.current = setInterval(() => {
      setElapsedMs(performance.now() - startedAtRef.current);
    }, 80);
    return true;
  }, [cleanupRecorder, onError, onUnavailable, releaseStream, reset, sending, starting]);

  /** 上滑取消臂：由按压组件在 pointermove 时调用。 */
  const setDragCancel = useCallback((armed: boolean) => {
    if (cancelRef.current === armed) return;
    cancelRef.current = armed;
    setCancelArmed(armed);
  }, []);

  const discard = useCallback(() => {
    pressedRef.current = false;
    const recorder = recorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      recorder.onstop = null;
      try {
        recorder.stop();
      } catch {
        /* ignore */
      }
    }
    reset();
  }, [reset]);

  /** 松手：有效则回调，否则丢弃。 */
  const finish = useCallback(async () => {
    pressedRef.current = false;
    const recorder = recorderRef.current;
    if (!recorder) return;

    const durationMs = performance.now() - startedAtRef.current;
    const cancelled = cancelRef.current;

    if (cancelled) {
      discard();
      return;
    }

    stopTicker();
    setRecording(false);

    if (durationMs < MIN_DURATION_MS || chunksRef.current.length === 0) {
      reset();
      onError?.("说话时间太短，按住 0.4 秒以上再松手");
      return;
    }

    const mime = recorder.mimeType || pickMime();
    const stopped = new Promise<Blob | null>((resolve) => {
      recorder.onstop = () => {
        const blob = chunksRef.current.length > 0
          ? new Blob(chunksRef.current, { type: mime || "audio/webm" })
          : null;
        resolve(blob && blob.size > 0 ? blob : null);
      };
      try {
        recorder.stop();
      } catch {
        resolve(null);
      }
    });

    const blob = await stopped;
    releaseStream();
    recorderRef.current = null;
    setElapsedMs(0);

    if (!blob) {
      reset();
      onError?.("没有录到声音，请重试");
      return;
    }

    setSending(true);
    try {
      await onComplete({ blob, durationMs, filename: filenameFor(mime) });
    } finally {
      if (mountedRef.current) setSending(false);
      setElapsedMs(0);
    }
  }, [discard, onComplete, onError, releaseStream, reset, stopTicker]);

  const cancel = useCallback(() => {
    discard();
  }, [discard]);

  useEffect(() => {
    return () => {
      const recorder = recorderRef.current;
      if (recorder && recorder.state !== "inactive") {
        recorder.onstop = null;
        try {
          recorder.stop();
        } catch {
          /* ignore */
        }
      }
      cleanupRecorder();
    };
  }, [cleanupRecorder]);

  return {
    /** 麦克风可用（预检结果，false 时仍可尝试一次） */
    supported,
    probing,
    recording,
    starting,
    sending,
    elapsedMs,
    cancelArmed,
    start,
    finish,
    cancel,
    setDragCancel,
    minDurationMs: MIN_DURATION_MS,
    cancelDragPx: CANCEL_DRAG_PX,
  };
}
