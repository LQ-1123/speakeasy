import { nativeBridge } from "./v3/clientApi";
import { usePreferences } from "./v3/preferences";
import { callSession } from "./v3/callSession";

export function unlockNotifyAudio(): void { /* User gestures unlock regular media playback. */ }
function play(kind: "message" | "request"): void {
  if (!usePreferences.getState().value.sound || callSession.getSnapshot()) return;
  const native = nativeBridge();
  if (native?.playSound) { native.playSound(kind); return; }
  const audio = new Audio(`/sounds/${kind}.wav`);
  audio.volume = 0.5;
  void audio.play().catch(() => undefined);
}
export function playMessageSound(): void { play("message"); }
export function playRequestSound(): void { play("request"); }
