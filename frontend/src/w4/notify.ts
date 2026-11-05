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

function bell(notes: Array<{ f: number; at: number; dur: number; gain: number }>): void {
  const ac = ensureCtx();
  if (!ac) return;
  const base = ac.currentTime + 0.01;
  for (const n of notes) {
    const t = base + n.at;
    const osc = ac.createOscillator();
    const h2 = ac.createOscillator();
    const h3 = ac.createOscillator();
    const gain = ac.createGain();
    const g2 = ac.createGain();
    const g3 = ac.createGain();
    osc.type = "sine";
    h2.type = "sine";
    h3.type = "sine";
    osc.frequency.value = n.f;
    h2.frequency.value = n.f * 2.01;
    h3.frequency.value = n.f * 3.02;
    g2.gain.value = 0.32;
    g3.gain.value = 0.14;
    // 指数衰减，接近铃声 / 钢琴质感
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(Math.max(0.02, n.gain), t + 0.008);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + n.dur);
    osc.connect(gain);
    h2.connect(g2);
    g2.connect(gain);
    h3.connect(g3);
    g3.connect(gain);
    gain.connect(ac.destination);
    for (const o of [osc, h2, h3]) {
      o.start(t);
      o.stop(t + n.dur + 0.02);
    }
  }
}

/** 新消息：一声「叮」 */
export function playMessageSound(): void {
  const nat = nativeTone();
  if (nat?.ding) {
    nat.ding();
    return;
  }
  bell([{ f: 1318.5, at: 0, dur: 0.55, gain: 0.5 }, { f: 2637, at: 0, dur: 0.3, gain: 0.12 }]);
}

/** 新好友申请：两声上行 */
export function playRequestSound(): void {
  const nat = nativeTone();
  if (nat?.dong) {
    nat.dong();
    return;
  }
  bell([
    { f: 880, at: 0, dur: 0.45, gain: 0.5 },
    { f: 1318.5, at: 0.22, dur: 0.55, gain: 0.48 },
  ]);
}
