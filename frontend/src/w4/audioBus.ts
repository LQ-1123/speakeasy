/**
 * 全局音频总线：同一时刻只允许一个声音在播。
 *
 * 为什么需要：朗读/试听/预生成音频分散在多个组件里，各自 `new Audio()`，
 * 点下一句时上一句不会停，会两句话重叠。这里统一拦截 HTMLMediaElement.play，
 * 播放新音频前先暂停旧的，并广播「开始播放」事件让各组件重置按钮状态。
 */

const isWebAudioPlaying = () => false;
const stopWebAudio = () => undefined;
const webAudioStats = () => ({ playing: false, duration: 0, currentTime: 0 });

type StartListener = (src: string) => void;

let current: HTMLMediaElement | null = null;
let installed = false;
const listeners = new Set<StartListener>();

function notify(src: string) {
  for (const fn of listeners) {
    try {
      fn(src);
    } catch {
      /* 单个监听器异常不影响播放 */
    }
  }
}

export function installAudioBus(): void {
  if (installed || typeof window === "undefined") return;
  const proto = window.HTMLMediaElement && window.HTMLMediaElement.prototype;
  if (!proto) return;
  installed = true;

  const origPlay = proto.play;
  proto.play = function patchedPlay(this: HTMLMediaElement) {
    if (current && current !== this) {
      try {
        current.pause();
        current.currentTime = 0;
      } catch {
        /* 忽略 */
      }
    }
    current = this;
    stopWebAudio();
    notify((this as HTMLAudioElement).src || "");
    return origPlay.call(this);
  };

  const origPause = proto.pause;
  proto.pause = function patchedPause(this: HTMLMediaElement) {
    if (current === this) current = null;
    return origPause.call(this);
  };

  /* 便于排查：控制台可看当前音频 */
  (window as unknown as Record<string, unknown>).__speakeasyAudio = {
    currentSrc: () => (current as HTMLAudioElement | null)?.src || "",
    paused: () => (current as HTMLAudioElement | null)?.paused ?? true,
    webAudio: () => isWebAudioPlaying(),
    stats: () => webAudioStats(),
    stop: () => stopAudio(),
  };
}

/** 订阅「某个音频开始播放」，返回取消订阅函数。 */
export function onAudioStart(fn: StartListener): () => void {
  installAudioBus();
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

export function currentAudioSrc(): string {
  return (current as HTMLAudioElement | null)?.src || "";
}

/** 立即停止当前音频（例如离开页面时）。 */
export function stopAudio(): void {
  stopWebAudio();
  if (!current) return;
  try {
    current.pause();
    current.currentTime = 0;
  } catch {
    /* 忽略 */
  }
  current = null;
}
