/** 本地模式：直接调用 SiliconFlow 做语音转文字（SenseVoice）。 */

import { loadKeys } from "./keys";

export async function transcribeBlob(blob: Blob): Promise<string> {
  const k = loadKeys();
  if (!k.sttKey.trim()) throw new Error("未配置 SiliconFlow Key");
  const form = new FormData();
  form.append("model", k.sttModel || "FunAudioLLM/SenseVoiceSmall");
  form.append("file", blob, blob.type.includes("ogg") ? "voice.ogg" : "voice.webm");
  const resp = await fetch(`${k.sttBase.replace(/\/+$/, "")}/audio/transcriptions`, {
    method: "POST",
    headers: { Authorization: `Bearer ${k.sttKey.trim()}` },
    body: form,
  });
  if (!resp.ok) {
    const detail = await resp.text().catch(() => "");
    throw new Error(`SiliconFlow ${resp.status}: ${detail.slice(0, 160)}`);
  }
  const data = (await resp.json()) as { text?: string };
  return (data.text ?? "").trim();
}
