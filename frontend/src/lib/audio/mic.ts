"use client";

import { FRAME_SAMPLES, IN_SAMPLE_RATE } from "@/lib/types";

const WORKLET_URL = "/worklets/pcm-processor.js";

export type FrameHandler = (frame: ArrayBuffer, rms: number) => void;

export interface MicOptions {
  onFrame: FrameHandler;
  onError?: (message: string) => void;
  onLevel?: (rms: number) => void;
  /** 指定输入设备（deviceId）。空 = 用系统默认。 */
  deviceId?: string;
}

/** 枚举可用音频输入设备。需要先拿到过权限，否则 label 为空。 */
export async function listMicrophones(): Promise<MediaDeviceInfo[]> {
  if (typeof navigator === "undefined" || !navigator.mediaDevices?.enumerateDevices) return [];
  try {
    const all = await navigator.mediaDevices.enumerateDevices();
    return all.filter((d) => d.kind === "audioinput");
  } catch {
    return [];
  }
}

/**
 * 麦克风采集：
 *  - `AudioContext({ sampleRate: 16000 })` 优先；浏览器接受则该 ctx 直接跑 16k，
 *    重采样比例为 1。
 *  - 若浏览器拒绝/忽略 16k（Safari 常见），ctx 会跑在 44.1k/48k，
 *    此时把 **实际** `ctx.sampleRate` 传给 worklet，由 worklet 内部线性重采样到 16k。
 *  - 无论哪条路径，`onFrame` 收到的都是 **16 kHz 单声道 PCM16LE**，
 *    每帧恰好 `FRAME_SAMPLES`(320) 个采样 = 20 ms。
 */
export class MicCapture {
  private ctx: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private sourceNode: MediaStreamAudioSourceNode | null = null;
  private workletNode: AudioWorkletNode | null = null;
  private analyser: AnalyserNode | null = null;

  private timeDomain: Uint8Array<ArrayBuffer> | null = null;
  private readonly options: MicOptions;

  private _running = false;
  private _effectiveRate = IN_SAMPLE_RATE;

  constructor(options: MicOptions) {
    this.options = options;
  }

  get running(): boolean {
    return this._running;
  }

  /** 实际采集采样率（原生 16k 或降级前的硬件采样率），用于 UI 展示。 */
  get inputSampleRate(): number {
    return this._effectiveRate;
  }

  get context(): AudioContext | null {
    return this.ctx;
  }

  async start(): Promise<void> {
    if (this._running) return;

    if (typeof navigator === "undefined" || !navigator.mediaDevices?.getUserMedia) {
      throw new Error("当前浏览器不支持麦克风采集（需要 http://localhost 或 https）");
    }

    // 指定了设备就用指定设备；否则用系统默认。
    // 注意：本机存在 VB-Cable / Oculus / Steam 等虚拟声卡，
    // 系统默认输入可能指向它们（听不到真实人声），所以支持显式选择。
    const audioConstraints: MediaTrackConstraints = {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    };
    if (this.options.deviceId) {
      audioConstraints.deviceId = { exact: this.options.deviceId };
    }

    this.stream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraints });

    const ctx = await this._createContext();
    this.ctx = ctx;
    this._effectiveRate = ctx.sampleRate;

    if (ctx.state === "suspended") await ctx.resume();

    // worklet 模块只加载一次（同一 ctx 内重复 addModule 是幂等的，但避免多余往返）
    if (!ctx.audioWorklet) {
      throw new Error("当前浏览器不支持 AudioWorklet，请使用 Chrome / Edge / Firefox");
    }
    await ctx.audioWorklet.addModule(WORKLET_URL);

    this.sourceNode = ctx.createMediaStreamSource(this.stream);

    this.workletNode = new AudioWorkletNode(ctx, "pcm-processor", {
      numberOfInputs: 1,
      numberOfOutputs: 0,
      channelCount: 1,
      channelCountMode: "explicit",
      processorOptions: {
        targetRate: IN_SAMPLE_RATE,
        frameSamples: FRAME_SAMPLES,
        inputRate: ctx.sampleRate,
      },
    });

    this.workletNode.port.onmessage = (event: MessageEvent) => {
      const data = event.data as { pcm?: ArrayBuffer; rms?: number } | null;
      if (!data || !(data.pcm instanceof ArrayBuffer)) return;
      const rms = typeof data.rms === "number" ? data.rms : 0;
      this.options.onFrame(data.pcm, rms);
    };
    this.workletNode.onprocessorerror = () => {
      this.options.onError?.("音频处理器异常，请刷新页面重试");
    };

    // 上行波形用的时域分析器（不影响 worklet 链路）
    this.analyser = ctx.createAnalyser();
    this.analyser.fftSize = 2048;
    this.analyser.smoothingTimeConstant = 0.5;
    this.timeDomain = new Uint8Array(new ArrayBuffer(this.analyser.fftSize));

    this.sourceNode.connect(this.analyser);
    this.sourceNode.connect(this.workletNode);

    this._running = true;
    this.workletNode.port.postMessage({ cmd: "start" });
  }

  /** 选出最合适的 AudioContext：优先原生 16k，失败则退回默认采样率。 */
  private async _createContext(): Promise<AudioContext> {
    const Ctor: typeof AudioContext =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;

    if (!Ctor) throw new Error("当前浏览器不支持 Web Audio API");

    try {
      const preferred = new Ctor({ sampleRate: IN_SAMPLE_RATE, latencyHint: "interactive" });
      // 有些浏览器不报错但会静默改成硬件采样率，这里不做强制，交给 worklet 重采样
      return preferred;
    } catch {
      return new Ctor({ latencyHint: "interactive" });
    }
  }

  /** 上行波形：返回 AnalyserNode 的时域数据（0..255，128 为零点）。 */
  readWaveform(target: Uint8Array<ArrayBuffer>): boolean {
    if (!this.analyser || !this.timeDomain) return false;
    this.analyser.getByteTimeDomainData(this.timeDomain);
    target.set(this.timeDomain.subarray(0, Math.min(target.length, this.timeDomain.length)));
    return true;
  }

  /** 软停止：停止推帧但保留麦克风与 ctx（便于快速再次开口）。 */
  stop(): void {
    if (!this._running) return;
    this._running = false;
    this.workletNode?.port.postMessage({ cmd: "flush" });
    this.workletNode?.port.postMessage({ cmd: "stop" });
  }

  async dispose(): Promise<void> {
    this._running = false;
    try {
      this.workletNode?.port.postMessage({ cmd: "stop" });
      this.workletNode?.port.close();
    } catch {
      /* 忽略关闭期异常 */
    }
    this.workletNode?.disconnect();
    this.workletNode = null;

    this.analyser?.disconnect();
    this.analyser = null;
    this.timeDomain = null;

    this.sourceNode?.disconnect();
    this.sourceNode = null;

    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = null;

    const ctx = this.ctx;
    this.ctx = null;
    if (ctx && ctx.state !== "closed") {
      try {
        await ctx.close();
      } catch {
        /* 忽略 */
      }
    }
  }
}
