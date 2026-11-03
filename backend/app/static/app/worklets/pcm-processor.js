/**
 * PCM 采集 AudioWorkletProcessor。
 *
 * 职责（全部在音频线程完成，避免主线程抖动）：
 *  1. 多声道下混成单声道。
 *  2. 线性重采样到 **16000 Hz**。AudioContext 本身跑在 16k 时比例 = 1，直接透传（零误差）；
 *     Safari 等不支持 16k 的浏览器由这里降采样。
 *  3. 按 320 samples（20 ms @16k）切帧，Float32 → Int16LE 打包，转移所有权 postMessage。
 *
 * 主线程指令：`start` / `stop` / `flush`（把不足一帧的残留补零立即推出）。
 *
 * 重采样实现：维护"未消费输入缓冲 `_buf` + 小数位置 `_pos`"，
 * 只要 `_pos + 1 < _buf.length` 就能安全做线性插值；随后丢弃已消费的整数部分。
 * 该写法在跨 `process()` 调用时相位连续，帧边界不会累积误差。
 */

const TARGET_RATE = 16000;
const FRAME_SAMPLES = 320;

class PcmProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};

    this._targetRate = typeof opts.targetRate === "number" ? opts.targetRate : TARGET_RATE;
    this._frameSamples = typeof opts.frameSamples === "number" ? opts.frameSamples : FRAME_SAMPLES;
    this._inputRate = typeof opts.inputRate === "number" && opts.inputRate > 0
      ? opts.inputRate
      : (typeof sampleRate === "number" ? sampleRate : TARGET_RATE);
    this._ratio = this._inputRate / this._targetRate;

    this._running = false;

    /** 未消费的输入样本（已下混为单声道） */
    this._buf = new Float32Array(0);
    /** 下一个输出样本在 _buf 中的小数位置 */
    this._pos = 0;

    /** 待推送帧累积器 */
    this._acc = new Float32Array(this._frameSamples);
    this._accLen = 0;

    this.port.onmessage = (event) => this._onCommand(event.data);
  }

  _onCommand(data) {
    if (!data || typeof data !== "object") return;
    switch (data.cmd) {
      case "start":
        this._buf = new Float32Array(0);
        this._pos = 0;
        this._accLen = 0;
        this._running = true;
        break;
      case "stop":
        this._running = false;
        break;
      case "flush":
        if (this._accLen > 0) {
          // 补零到整帧再发，保证上行帧长恒为 320 samples
          this._acc.fill(0, this._accLen);
          this._emitFrame(this._frameSamples);
          this._accLen = 0;
        }
        break;
      default:
        break;
    }
  }

  _downmix(input) {
    const frames = input[0].length;
    if (input.length === 1) return input[0];

    const mono = new Float32Array(frames);
    for (let ch = 0; ch < input.length; ch += 1) {
      const data = input[ch];
      for (let i = 0; i < frames; i += 1) mono[i] += data[i];
    }
    const scale = 1 / input.length;
    for (let i = 0; i < frames; i += 1) mono[i] *= scale;
    return mono;
  }

  _resample(mono) {
    if (this._ratio === 1) return mono;

    // 1) 拼接未消费缓冲 + 新样本
    const merged = new Float32Array(this._buf.length + mono.length);
    merged.set(this._buf, 0);
    merged.set(mono, this._buf.length);

    // 2) 按小数位置线性插值
    const out = [];
    let pos = this._pos;
    while (pos + 1 < merged.length) {
      const idx = Math.floor(pos);
      const frac = pos - idx;
      const a = merged[idx];
      const b = merged[idx + 1];
      out.push(a + (b - a) * frac);
      pos += this._ratio;
    }

    // 3) 丢弃已消费的整数部分，保留小数相位
    const consumed = Math.floor(pos);
    this._buf = merged.slice(Math.min(consumed, merged.length));
    this._pos = pos - consumed;

    return out.length > 0 ? Float32Array.from(out) : new Float32Array(0);
  }

  _pushSamples(samples) {
    let offset = 0;
    while (offset < samples.length) {
      const room = this._frameSamples - this._accLen;
      const take = Math.min(room, samples.length - offset);
      this._acc.set(samples.subarray(offset, offset + take), this._accLen);
      this._accLen += take;
      offset += take;
      if (this._accLen === this._frameSamples) {
        this._emitFrame(this._frameSamples);
        this._accLen = 0;
      }
    }
  }

  _emitFrame(length) {
    const pcm = new ArrayBuffer(length * 2);
    const view = new DataView(pcm);
    let sumSq = 0;
    for (let i = 0; i < length; i += 1) {
      const clamped = Math.max(-1, Math.min(1, this._acc[i]));
      sumSq += clamped * clamped;
      // 负半轴用 0x8000、正半轴用 0x7fff：标准的非对称 Int16 量化
      view.setInt16(i * 2, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
    }
    const rms = Math.sqrt(sumSq / Math.max(1, length));
    this.port.postMessage({ pcm, rms, samples: length }, [pcm]);
  }

  process(inputs) {
    const input = inputs[0];
    if (!this._running || !input || input.length === 0 || !input[0] || input[0].length === 0) {
      return true; // 保持节点存活
    }
    const mono = this._downmix(input);
    const resampled = this._resample(mono);
    if (resampled.length > 0) this._pushSamples(resampled);
    return true;
  }
}

registerProcessor("pcm-processor", PcmProcessor);
