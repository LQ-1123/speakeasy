type NativeTone = { ding?: () => void; dong?: () => void };

function nativeTone(): NativeTone | undefined {
  return (window as unknown as { AndroidTone?: NativeTone }).AndroidTone;
}

/** 提示音：用 WebAudio 现场合成，不依赖音频文件（APK 内也能响）。 */

let ctx: AudioContext | null = null;

function ensureCtx(): AudioContext | null {
  try {
    ctx = ctx ?? new AudioContext();
    if (ctx.state === "suspended") void ctx.resume();
    return ctx;
  } catch {
    return null;
  }
}

/** 首次用户交互时解锁音频（移动端自动播放策略） */
export function unlockNotifyAudio(): void {
  ensureCtx();
}

function tone(freqs: number[], duration = 0.12, gap = 0.09, volume = 0.05): void {
  const ac = ensureCtx();
  if (!ac) return;
  let t = ac.currentTime + 0.01;
  for (const f of freqs) {
    const osc = ac.createOscillator();
    const gain = ac.createGain();
    osc.type = "sine";
    osc.frequency.value = f;
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(volume, t + 0.015);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + duration);
    osc.connect(gain);
    gain.connect(ac.destination);
    osc.start(t);
    osc.stop(t + duration + 0.02);
    t += gap;
  }
}

/** 新消息：一声「叮」 */
export function playMessageSound(): void {
  const nat = nativeTone();
  if (nat?.ding) {
    nat.ding();
    return;
  }
  tone([880], 0.11, 0, 0.05);
}

/** 新好友申请：两声上行 */
export function playRequestSound(): void {
  const nat = nativeTone();
  if (nat?.dong) {
    nat.dong();
    return;
  }
  tone([660, 990], 0.11, 0.1, 0.06);
}
