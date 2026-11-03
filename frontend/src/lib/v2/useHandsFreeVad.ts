"use client";

/**
 * 免提通话 VAD 录音器（hands-free）。
 *
 * 思路：
 * - 进入通话时 `getUserMedia` **一次**，持续持有 stream；
 * - Web Audio AnalyserNode 每 50ms 计算 RMS 做 VAD：先 500ms 噪声校准
 *   （threshold = max(noise*2.5, 0.015)），检测到语音后启动 MediaRecorder 分段录音，
 *   语音结束（连续 1.2s 静音）自动停止并回调 `onUtterance`，无需任何按键；
 * - 好友语音播放期间通过 `enabled=false` 暂停监听/录音，避免把 TTS 录进去；
 * - 不支持（无 MediaRecorder/Web Audio）或麦克风被拒绝时通过回调上报，UI 降级；
 * - 卸载时停止录音、关闭 AudioContext、停止 tracks。
 */

import { useCallback, useEffect, useRef, useState } from "react";

export interface VadUtterance {
  blob: Blob;
  durationMs: number;
  filename: string;
}

export type VadPhase =
  | "initializing"
  | "calibrating"
  | "listening"
  | "recording"
  | "paused"
  | "unsupported"
  | "denied";

export interface UseHandsFreeVadOptions {
  /** false 时暂停监听（静音 / 对方说话 / 处理中 / 已挂断） */
  enabled: boolean;
  /** 检测到一句完整语音（自动断句） */
  onUtterance: (utterance: VadUtterance) => void | Promise<void>;
  onUnavailable?: (message: string) => void;
  onDenied?: (message: string) => void;
  onError?: (message: string) => void;
}

const CALIBRATION_MS = 500;
const MIN_SPEECH_MS = 300;
const SILENCE_MS = 1200;
const MAX_UTTERANCE_MS = 30000;
const TICK_MS = 50;
const MIME_CANDIDATES = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];

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

export function useHandsFreeVad({
  enabled,
  onUtterance,
  onUnavailable,
  onDenied,
  onError,
}: UseHandsFreeVadOptions) {
  const [phase, setPhase] = useState<VadPhase>("initializing");
  const [level, setLevel] = useState(0);

  const enabledRef = useRef(enabled);
  const phaseRef = useRef<VadPhase>("initializing");
  const mountedRef = useRef(true);

  const streamRef = useRef<MediaStream | null>(null);
  const ctxRef = useRef<AudioContext | null>(null);
  const sourceRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const bufferRef = useRef<Float32Array<ArrayBuffer> | null>(null);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const mimeRef = useRef("");
  const speechStartedAtRef = useRef(0);
  const speechDurationMsRef = useRef(0);
  const lastVoiceAtRef = useRef(0);
  const shouldSendRef = useRef(false);

  const calibratingRef = useRef(false);
  const calibratedRef = useRef(false);
  const calibrationStartedAtRef = useRef(0);
  const calibrationSamplesRef = useRef<number[]>([]);
  const thresholdRef = useRef(0.02);

  const setupPromiseRef = useRef<Promise<boolean> | null>(null);
  const tickerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const onUtteranceRef = useRef(onUtterance);
  onUtteranceRef.current = onUtterance;
  const onUnavailableRef = useRef(onUnavailable);
  onUnavailableRef.current = onUnavailable;
  const onDeniedRef = useRef(onDenied);
  onDeniedRef.current = onDenied;
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;

  const updatePhase = useCallback((next: VadPhase) => {
    phaseRef.current = next;
    if (mountedRef.current) setPhase(next);
  }, []);

  const stopRecorder = useCallback(
    (send: boolean) => {
      const recorder = recorderRef.current;
      recorderRef.current = null;
      shouldSendRef.current = send;
      if (!recorder) return;
      recorder.onstop = () => {
        const chunks = chunksRef.current;
        chunksRef.current = [];
        if (!shouldSendRef.current || !mountedRef.current) {
          if (phaseRef.current !== "paused") updatePhase("listening");
          return;
        }
        const mime = recorder.mimeType || mimeRef.current || "audio/webm";
        const blob = chunks.length > 0 ? new Blob(chunks, { type: mime }) : null;
        const durationMs = speechDurationMsRef.current;
        shouldSendRef.current = false;
        if (!blob || blob.size === 0 || durationMs < MIN_SPEECH_MS) {
          updatePhase("listening");
          return;
        }
        updatePhase("paused");
        void Promise.resolve(
          onUtteranceRef.current({ blob, durationMs, filename: filenameFor(mime) }),
        ).catch((err: unknown) => {
          onErrorRef.current?.(err instanceof Error ? err.message : "语音没有送达，请再说一次");
        });
      };
      try {
        recorder.stop();
      } catch {
        recorderRef.current = null;
        updatePhase("listening");
      }
    },
    [updatePhase],
  );

  const startRecorder = useCallback(() => {
    const stream = streamRef.current;
    if (!stream) return;
    const mime = pickMime();
    mimeRef.current = mime;
    let recorder: MediaRecorder;
    try {
      recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    } catch {
      onUnavailableRef.current?.("当前浏览器无法分段录音，已切换为按住说话");
      updatePhase("unsupported");
      return;
    }
    chunksRef.current = [];
    recorder.ondataavailable = (event: BlobEvent) => {
      if (event.data && event.data.size > 0) chunksRef.current.push(event.data);
    };
    recorder.onerror = () => {
      onErrorRef.current?.("录音失败，请检查麦克风");
    };
    recorderRef.current = recorder;
    try {
      recorder.start(200);
    } catch {
      recorderRef.current = null;
      onErrorRef.current?.("录音启动失败");
      return;
    }
    speechStartedAtRef.current = performance.now();
    speechDurationMsRef.current = 0;
    lastVoiceAtRef.current = performance.now();
    updatePhase("recording");
  }, [updatePhase]);

  const cleanup = useCallback(() => {
    if (tickerRef.current) clearInterval(tickerRef.current);
    tickerRef.current = null;
    const recorder = recorderRef.current;
    recorderRef.current = null;
    shouldSendRef.current = false;
    if (recorder && recorder.state !== "inactive") {
      try {
        recorder.stop();
      } catch {
        /* ignore */
      }
    }
    try {
      sourceRef.current?.disconnect();
    } catch {
      /* ignore */
    }
    sourceRef.current = null;
    analyserRef.current = null;
    bufferRef.current = null;
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    const ctx = ctxRef.current;
    ctxRef.current = null;
    if (ctx && ctx.state !== "closed") {
      void ctx.close().catch(() => undefined);
    }
  }, []);

  const ensureAudio = useCallback(async (): Promise<boolean> => {
    if (streamRef.current && ctxRef.current && analyserRef.current) return true;
    if (setupPromiseRef.current) return setupPromiseRef.current;

    const setup = (async () => {
      if (
        typeof navigator === "undefined" ||
        !navigator.mediaDevices?.getUserMedia ||
        typeof MediaRecorder === "undefined" ||
        typeof AudioContext === "undefined"
      ) {
        updatePhase("unsupported");
        onUnavailableRef.current?.("当前浏览器不支持免提语音，已切换为按住说话");
        return false;
      }
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            channelCount: 1,
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          },
        });
        if (!mountedRef.current) {
          stream.getTracks().forEach((track) => track.stop());
          return false;
        }
        const ctx = new AudioContext();
        try {
          await ctx.resume();
        } catch {
          /* 允许在首次手势中恢复，失败时后续 pointerdown 再试 */
        }
        const source = ctx.createMediaStreamSource(stream);
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 1024;
        analyser.smoothingTimeConstant = 0.4;
        source.connect(analyser);

        streamRef.current = stream;
        ctxRef.current = ctx;
        sourceRef.current = source;
        analyserRef.current = analyser;
        bufferRef.current = new Float32Array(new ArrayBuffer(analyser.fftSize * 4));
        calibratedRef.current = false;
        startCalibrationInternal();
        return true;
      } catch (err) {
        const name = err instanceof DOMException ? err.name : "";
        updatePhase("denied");
        if (name === "NotAllowedError" || name === "SecurityError") {
          onDeniedRef.current?.("麦克风权限被拒绝，已结束监听；仍可结束通话");
        } else if (name === "NotFoundError" || name === "DevicesNotFoundError") {
          onUnavailableRef.current?.("没有检测到麦克风，已切换为按住说话");
        } else {
          onUnavailableRef.current?.("无法使用麦克风，已切换为按住说话");
        }
        return false;
      }
    })();
    setupPromiseRef.current = setup;
    const ok = await setup;
    if (!ok) setupPromiseRef.current = null;
    return ok;
  }, [updatePhase]);

  function startCalibrationInternal() {
    calibratingRef.current = true;
    calibratedRef.current = false;
    calibrationSamplesRef.current = [];
    calibrationStartedAtRef.current = performance.now();
    updatePhase("calibrating");
  }

  /** 手动重试麦克风（例如权限刚被允许） */
  const retry = useCallback(async () => {
    cleanup();
    setupPromiseRef.current = null;
    calibratedRef.current = false;
    updatePhase("initializing");
    const ok = await ensureAudio();
    if (ok) startCalibrationInternal();
  }, [cleanup, ensureAudio, updatePhase]);

  /** 首次用户手势恢复 AudioContext（浏览器自动播放策略） */
  const resume = useCallback(async () => {
    const ctx = ctxRef.current;
    if (ctx && ctx.state === "suspended") {
      try {
        await ctx.resume();
      } catch {
        /* ignore */
      }
    }
  }, []);

  /* ------------------------------- VAD 主循环 ------------------------------- */

  useEffect(() => {
    mountedRef.current = true;

    const tick = () => {
      const analyser = analyserRef.current;
      const buffer = bufferRef.current;
      if (!analyser || !buffer) return;
      try {
        analyser.getFloatTimeDomainData(buffer);
      } catch {
        return;
      }
      let sum = 0;
      for (let i = 0; i < buffer.length; i += 1) sum += buffer[i] * buffer[i];
      const rms = Math.sqrt(sum / buffer.length);
      if (mountedRef.current) {
        setLevel((prev) => {
          const next = Math.min(1, rms * 9);
          return Math.abs(next - prev) > 0.015 ? next : prev;
        });
      }

      if (!enabledRef.current) {
        // 暂停期间不做任何判断（并丢弃正在录的片段）
        if (recorderRef.current) stopRecorder(false);
        return;
      }

      const now = performance.now();
      if (calibratingRef.current) {
        calibrationSamplesRef.current.push(rms);
        if (now - calibrationStartedAtRef.current >= CALIBRATION_MS) {
          const samples = calibrationSamplesRef.current;
          const noise = samples.length > 0 ? samples.reduce((a, b) => a + b, 0) / samples.length : 0;
          thresholdRef.current = Math.max(noise * 2.5, 0.015);
          calibratingRef.current = false;
          calibratedRef.current = true;
          updatePhase(recorderRef.current ? "recording" : "listening");
        }
        return;
      }

      const isVoice = rms > thresholdRef.current;
      const recorder = recorderRef.current;
      if (!recorder) {
        if (isVoice) {
          startRecorder();
        }
        return;
      }
      if (isVoice) {
        lastVoiceAtRef.current = now;
        speechDurationMsRef.current = now - speechStartedAtRef.current;
      }
      const silencedFor = now - lastVoiceAtRef.current;
      if (silencedFor >= SILENCE_MS) {
        const duration = speechDurationMsRef.current;
        if (duration >= MIN_SPEECH_MS) stopRecorder(true);
        else stopRecorder(false);
        return;
      }
      if (now - speechStartedAtRef.current >= MAX_UTTERANCE_MS) {
        stopRecorder(true);
      }
    };

    tickerRef.current = setInterval(tick, TICK_MS);
    return () => {
      mountedRef.current = false;
      cleanup();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* --------------------------- enabled 变化时启停监听 --------------------------- */

  useEffect(() => {
    enabledRef.current = enabled;
    if (!enabled) {
      if (recorderRef.current) stopRecorder(false);
      if (
        phaseRef.current !== "unsupported" &&
        phaseRef.current !== "denied" &&
        phaseRef.current !== "initializing"
      ) {
        updatePhase("paused");
      }
      return;
    }
    void (async () => {
      const ok = await ensureAudio();
      if (!ok || !mountedRef.current) return;
      if (!calibratedRef.current) startCalibrationInternal();
      else if (!recorderRef.current) updatePhase("listening");
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled]);

  return { phase, level, retry, resume, supported: phase !== "unsupported" && phase !== "denied" };
}
