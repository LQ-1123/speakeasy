"use client";

import { OUT_SAMPLE_RATE } from "@/lib/types";

/** 排程提前量：留 150 ms 抖动缓冲，避免每帧都贴着 currentTime 导致爆音。 */
const SCHEDULE_AHEAD_S = 0.15;
/** 补静音上限：跳号时最多补 12 帧（≈240 ms），避免服务端异常导致长时间静默。 */
const MAX_GAP_FRAMES = 12;
/** 轮询排程时间线的间隔（驱动字幕高亮与播放结束判定）。 */
const TICK_MS = 40;

interface ScheduledSentenceMark {
  at: number;
  sentenceIndex: number;
  turnId: number;
}

export interface PlayerCallbacks {
  onSentenceChange?: (turnId: number, sentenceIndex: number) => void;
  /** 队列排空、本回合音频全部播完 */
  onDrain?: (turnId: number) => void;
  onStats?: (stats: PlayerStats) => void;
}

export interface PlayerStats {
  playing: boolean;
  droppedFrames: number;
  gapFrames: number;
  /** 已排程但未播完的音频秒数 */
  bufferedSeconds: number;
}

export class AudioPlayer {
  private ctx: AudioContext | null = null;
  private master: GainNode | null = null;
  private analyser: AnalyserNode | null = null;
  private timeDomain: Uint8Array<ArrayBuffer> | null = null;

  private readonly callbacks: PlayerCallbacks;

  /** 当前排程指针（AudioContext 时间轴） */
  private playhead = 0;
  /** 本回合下一个期望的 seq */
  private nextSeq = 0;
  private currentTurn = -1;

  private activeTurn = -1;
  private activeSentence = -1;
  private marks: ScheduledSentenceMark[] = [];

  private _droppedFrames = 0;
  private _gapFrames = 0;
  private _playing = false;
  private turnStreamEnded = false;
  private ticker: ReturnType<typeof setInterval> | null = null;

  constructor(callbacks: PlayerCallbacks = {}) {
    this.callbacks = callbacks;
  }

  get playing(): boolean {
    return this._playing;
  }

  get droppedFrames(): number {
    return this._droppedFrames;
  }

  get gapFrames(): number {
    return this._gapFrames;
  }

  get context(): AudioContext | null {
    return this.ctx;
  }

  /** 必须在用户手势里调用一次：创建 24 kHz 播放用独立 AudioContext。 */
  async unlock(): Promise<void> {
    const ctx = this.ensureContext();
    if (ctx.state === "suspended") await ctx.resume();
  }

  private ensureContext(): AudioContext {
    if (this.ctx) return this.ctx;

    const Ctor: typeof AudioContext =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    if (!Ctor) throw new Error("当前浏览器不支持 Web Audio API");

    // 播放用独立 ctx，固定 24 kHz（与录音 ctx 分离，避免采样率冲突）
    let ctx: AudioContext;
    try {
      ctx = new Ctor({ sampleRate: OUT_SAMPLE_RATE, latencyHint: "interactive" });
    } catch {
      ctx = new Ctor({ latencyHint: "interactive" });
    }

    this.ctx = ctx;
    this.master = ctx.createGain();
    this.master.gain.value = 1;

    this.analyser = ctx.createAnalyser();
    this.analyser.fftSize = 1024;
    this.analyser.smoothingTimeConstant = 0.4;
    this.timeDomain = new Uint8Array(new ArrayBuffer(this.analyser.fftSize));

    this.master.connect(this.analyser);
    this.analyser.connect(ctx.destination);

    this.playhead = ctx.currentTime;
    return ctx;
  }

  /** 新回合开始：重置 seq 与时间线（不清 ctx）。 */
  beginTurn(turnId: number): void {
    if (this.currentTurn === turnId) return;
    this.currentTurn = turnId;
    this.nextSeq = 0;
    this.turnStreamEnded = false;
  }

  /**
   * 入队一帧下行音频。
   * @param data base64 编码的 PCM16LE
   */
  async pushAudio(params: {
    turnId: number;
    sentenceIndex: number;
    seq: number;
    data: string;
    sampleRate: number;
  }): Promise<void> {
    const ctx = this.ensureContext();
    if (ctx.state === "suspended") {
      // 自动播放策略：无手势时先尝试恢复，失败则丢弃并计为丢帧（UI 会提示点击以解锁）
      try {
        await ctx.resume();
      } catch {
        this._droppedFrames += 1;
        this.emitStats();
        return;
      }
    }

    this.beginTurn(params.turnId);

    // seq 跳号：补静音并计数
    if (params.seq > this.nextSeq) {
      const missing = params.seq - this.nextSeq;
      this._gapFrames += missing;
      const toInsert = Math.min(missing, MAX_GAP_FRAMES);
      // 用标准 20ms 估计缺失时长
      const silenceSeconds = toInsert * 0.02;
      this.scheduleSilence(silenceSeconds, params.sentenceIndex);
    } else if (params.seq < this.nextSeq) {
      // 迟到/重复帧，丢弃
      this._droppedFrames += 1;
      this.emitStats();
      return;
    }
    this.nextSeq = params.seq + 1;

    const samples = decodeBase64Pcm16(params.data);
    if (!samples) {
      this._droppedFrames += 1;
      this.emitStats();
      return;
    }

    // 服务端声明 24k；若不同则使用实际 sample_rate 建 buffer（ctx 会自动重采样到其采样率）
    const rate = params.sampleRate > 0 ? params.sampleRate : OUT_SAMPLE_RATE;
    const buffer = ctx.createBuffer(1, samples.length, rate);
    buffer.copyToChannel(samples, 0);

    this.scheduleBuffer(buffer, params.turnId, params.sentenceIndex);
    this.startTicker();
    this.emitStats();
  }

  private scheduleSilence(seconds: number, sentenceIndex: number): void {
    const ctx = this.ensureContext();
    const len = Math.max(1, Math.round(seconds * ctx.sampleRate));
    const buffer = ctx.createBuffer(1, len, ctx.sampleRate);
    // createBuffer 已零填充，即静音
    this.scheduleBuffer(buffer, this.currentTurn, sentenceIndex);
  }

  private scheduleBuffer(buffer: AudioBuffer, turnId: number, sentenceIndex: number): void {
    const ctx = this.ensureContext();
    const master = this.master;
    if (!master) return;

    const now = ctx.currentTime;
    // 提前量排程：保证上次排程点已过时也不会"立即"起播造成咔哒声
    this.playhead = Math.max(this.playhead, now + SCHEDULE_AHEAD_S);
    const startAt = this.playhead;

    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(master);
    source.start(startAt);

    this.playhead = startAt + buffer.duration;

    this.marks.push({ at: startAt, sentenceIndex, turnId });
    // 只保留最近一批，避免长对话无界增长
    if (this.marks.length > 256) this.marks.splice(0, this.marks.length - 256);

    this._playing = true;
  }

  /** `turn_audio_end`：不再有新帧；等排程播完再通知 onDrain。 */
  endTurnAudio(turnId: number): void {
    this.turnStreamEnded = true;
    this.currentTurn = turnId;
    this.startTicker();
  }

  private startTicker(): void {
    if (this.ticker) return;
    this.ticker = setInterval(() => this.tick(), TICK_MS);
  }

  private stopTicker(): void {
    if (this.ticker) {
      clearInterval(this.ticker);
      this.ticker = null;
    }
  }

  private tick(): void {
    const ctx = this.ctx;
    if (!ctx) {
      this.stopTicker();
      return;
    }
    const now = ctx.currentTime;

    // 1) 字幕高亮：找到当前正在播的时间点
    let active: ScheduledSentenceMark | null = null;
    for (let i = this.marks.length - 1; i >= 0; i -= 1) {
      if (this.marks[i].at <= now + 0.01) {
        active = this.marks[i];
        break;
      }
    }
    if (active && (active.sentenceIndex !== this.activeSentence || active.turnId !== this.activeTurn)) {
      this.activeSentence = active.sentenceIndex;
      this.activeTurn = active.turnId;
      this.callbacks.onSentenceChange?.(active.turnId, active.sentenceIndex);
    }

    // 2) 清理已播完的 marks
    while (this.marks.length > 0 && this.marks[0].at < now - 2) this.marks.shift();

    // 3) 结束判定
    const drained = this.playhead <= now + 0.02;
    if (drained && this._playing) {
      this._playing = false;
      if (this.turnStreamEnded) {
        const turn = this.currentTurn;
        this.turnStreamEnded = false;
        this.marks = [];
        this.activeSentence = -1;
        this.callbacks.onDrain?.(turn);
      }
      this.emitStats();
    }

    if (!this._playing && this.marks.length === 0) this.stopTicker();
  }

  emitStats(): void {
    this.callbacks.onStats?.({
      playing: this._playing,
      droppedFrames: this._droppedFrames,
      gapFrames: this._gapFrames,
      bufferedSeconds: this.ctx ? Math.max(0, this.playhead - this.ctx.currentTime) : 0,
    });
  }

  /**
   * 打断：硬静音并清空排程。调用方负责 send({"type":"interrupt"})。
   *
   * 做两件事：
   *  1) `buys` 一个极短 ramp 把 master 增益打到 0 —— 避免直接断开造成爆音，
   *     同时让所有已排程 source 立刻静音（它们的 buffer 仍在播但不可闻）。
   *  2) 重建一条全新的 master 通路（gain 恢复 1，analyser 只连一次），
   *     旧 source 仍挂在**旧** master 上，随 playhead 重置被彻底弃用。
   *
   * 关键点：analyser 的 `analyser → destination` 只在 `ensureContext()` 里建立一次，
   * 这里绝不重复连接，否则音量会叠加。
   */
  flush(reason: "interrupt" | "reset" = "interrupt"): void {
    const ctx = this.ctx;

    if (ctx && this.master) {
      const now = ctx.currentTime;
      const oldMaster = this.master;
      try {
        const gain = oldMaster.gain;
        gain.cancelScheduledValues(now);
        gain.setValueAtTime(gain.value, now);
        gain.linearRampToValueAtTime(0, now + 0.03);
      } catch {
        /* 忽略：某些实现不支持 ramp 组合 */
      }
      // 30ms 后再断开旧 master，确保 ramp 生效且不产生瞬时噪声
      const stale = oldMaster;
      window.setTimeout(() => {
        try {
          stale.disconnect();
        } catch {
          /* 忽略 */
        }
      }, 80);

      const fresh = ctx.createGain();
      fresh.gain.value = 1;
      // analyser 已在 ensureContext 里连到 destination，这里只接 analyser 上游
      if (this.analyser) fresh.connect(this.analyser);
      else fresh.connect(ctx.destination);
      this.master = fresh;
    }

    this.playhead = ctx ? ctx.currentTime : 0;
    this.marks = [];
    this.activeSentence = -1;
    this.activeTurn = -1;
    this._playing = false;
    this.turnStreamEnded = false;
    this.nextSeq = 0;
    this.currentTurn = -1;
    if (reason === "reset") {
      this._droppedFrames = 0;
      this._gapFrames = 0;
    }
    this.stopTicker();
    this.emitStats();
  }

  /** 下行波形：返回 AnalyserNode 时域数据。 */
  readWaveform(target: Uint8Array<ArrayBuffer>): boolean {
    if (!this.analyser || !this.timeDomain) return false;
    this.analyser.getByteTimeDomainData(this.timeDomain);
    target.set(this.timeDomain.subarray(0, Math.min(target.length, this.timeDomain.length)));
    return true;
  }

  async dispose(): Promise<void> {
    this.stopTicker();
    this.flush("reset");
    const ctx = this.ctx;
    this.ctx = null;
    this.master = null;
    this.analyser = null;
    this.timeDomain = null;
    if (ctx && ctx.state !== "closed") {
      try {
        await ctx.close();
      } catch {
        /* 忽略 */
      }
    }
  }
}

/** base64 → Float32Array（PCM16LE 单声道）。解码失败返回 null。 */
export function decodeBase64Pcm16(data: string): Float32Array<ArrayBuffer> | null {
  if (typeof data !== "string" || data.length === 0) return null;

  let binary: string;
  try {
    binary = atob(data);
  } catch {
    return null;
  }

  const byteLength = binary.length;
  const raw = new ArrayBuffer(byteLength);
  const bytes = new Uint8Array(raw);
  for (let i = 0; i < byteLength; i += 1) bytes[i] = binary.charCodeAt(i);

  // 长度必须是 2 的倍数
  const usable = byteLength - (byteLength % 2);
  if (usable === 0) return null;

  const view = new DataView(raw, 0, usable);
  // 显式指定 ArrayBuffer 泛型，避免 copyToChannel 需要 Float32Array<ArrayBuffer>
  const out = new Float32Array(new ArrayBuffer((usable / 2) * 4));
  for (let i = 0; i < out.length; i += 1) {
    out[i] = view.getInt16(i * 2, true) / 0x8000;
  }
  return out;
}
