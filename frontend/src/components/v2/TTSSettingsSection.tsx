"use client";

/**
 * TTS 设置卡片：edge（免费）/ Fish Audio（S2.1 Pro，支持 [excited] 这类情绪标记）。
 *
 * 关键点
 * ----
 * * Key 只写不读：后端回显永远是打码值，所以输入框为空时表示「保持原 Key」
 * * 「试听」用当前已保存的配置合成一句带 [excited] 的样例，直接播出来
 * * Fish 失败时把原始报错显示出来（body 也显示），方便定位字段问题
 */

import { useCallback, useEffect, useState } from "react";

type VoiceItem = {
  id: string;
  title: string;
  description?: string;
  languages?: string[];
  tags?: string[];
};

type TtsView = {
  provider: "edge" | "fish";
  configured: boolean;
  fish_api_key: string;
  fish_base_url: string;
  fish_model: string;
  fish_reference_id: string;
  fish_format: string;
};

const EMPTY: TtsView = {
  provider: "edge",
  configured: false,
  fish_api_key: "",
  fish_base_url: "https://api.fish.audio",
  fish_model: "s2.1-pro-free",
  fish_reference_id: "",
  fish_format: "mp3",
};

const input =
  "w-full rounded-[8px] border border-[color:var(--se-line)] bg-white px-2.5 py-2 text-[12.5px] outline-none focus:border-[color:var(--se-accent)]";

export function TTSSettingsSection() {
  const [cfg, setCfg] = useState<TtsView>(EMPTY);
  const [provider, setProvider] = useState<"edge" | "fish">("edge");
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("s2.1-pro-free");
  const [refId, setRefId] = useState("");
  const [busy, setBusy] = useState<null | "save" | "test">(null);
  const [msg, setMsg] = useState<string>("");
  const [err, setErr] = useState<string>("");
  const [audioUrl, setAudioUrl] = useState<string>("");
  /* 音色列表（Fish 平台 1000+，服务端分页 + 过滤） */
  const [voices, setVoices] = useState<VoiceItem[]>([]);
  const [voiceQ, setVoiceQ] = useState("");
  const [voiceLang, setVoiceLang] = useState("en");
  const [voicesBusy, setVoicesBusy] = useState(false);
  const [voicesErr, setVoicesErr] = useState("");
  const [pickedTitle, setPickedTitle] = useState("");

  const load = useCallback(async () => {
    try {
      const r = await fetch("/api/v2/settings/tts", { cache: "no-store" });
      if (!r.ok) return;
      const j = (await r.json()) as { tts?: TtsView };
      const t = j.tts ?? EMPTY;
      setCfg(t);
      setProvider(t.provider);
      setModel(t.fish_model || "s2.1-pro-free");
      setRefId(t.fish_reference_id || "");
    } catch {
      /* 接口未就绪时保持默认值 */
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const loadVoices = useCallback(async (q: string, lang: string) => {
    setVoicesBusy(true);
    setVoicesErr("");
    try {
      const qs = new URLSearchParams({ q, language: lang, page: "1" });
      const r = await fetch(`/api/v2/settings/tts/voices?${qs}`, { cache: "no-store" });
      const j = (await r.json()) as { ok?: boolean; detail?: string; voices?: VoiceItem[] };
      if (!j.ok) {
        setVoicesErr(j.detail || "获取音色失败");
        setVoices([]);
        return;
      }
      setVoices(j.voices || []);
    } catch (e) {
      setVoicesErr(e instanceof Error ? e.message : String(e));
    } finally {
      setVoicesBusy(false);
    }
  }, []);

  // 切到 Fish 且已配置 Key 时，自动拉一次音色
  useEffect(() => {
    if (provider === "fish" && cfg.configured && voices.length === 0) {
      void loadVoices("", voiceLang);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provider, cfg.configured]);

  const save = async (nextProvider?: "edge" | "fish") => {
    setBusy("save");
    setErr("");
    setMsg("");
    try {
      const body: Record<string, unknown> = {
        provider: nextProvider ?? provider,
        model,
        reference_id: refId,
      };
      if (apiKey.trim()) body.api_key = apiKey.trim();
      const r = await fetch("/api/v2/settings/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const j = (await r.json()) as { tts?: TtsView; detail?: string };
      if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
      if (j.tts) {
        setCfg(j.tts);
        setProvider(j.tts.provider);
      }
      setApiKey("");
      setMsg("已保存，下一条语音就用新设置");
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const test = async () => {
    setBusy("test");
    setErr("");
    setMsg("");
    setAudioUrl("");
    try {
      const r = await fetch("/api/v2/settings/tts/test", { method: "POST" });
      const j = (await r.json()) as {
        ok?: boolean;
        detail?: string;
        fish_status?: number;
        fish_body?: string;
        audio_url?: string;
      };
      if (!j.ok) {
        setErr(`${j.detail || "合成失败"}${j.fish_status ? ` (HTTP ${j.fish_status})` : ""}`);
        if (j.fish_body) setErr((v) => `${v}\n${j.fish_body}`);
        return;
      }
      if (j.audio_url) setAudioUrl(j.audio_url);
      setMsg("合成成功，点下面的播放器听听");
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const reset = async () => {
    setBusy("save");
    setErr("");
    setMsg("");
    try {
      const r = await fetch("/api/v2/settings/tts/reset", { method: "POST" });
      const j = (await r.json()) as { tts?: TtsView };
      if (j.tts) {
        setCfg(j.tts);
        setProvider(j.tts.provider);
      }
      setApiKey("");
      setMsg("已恢复默认（edge 免费声线）");
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="se-setting-section">
      <div className="se-setting-head">
        <span className="se-setting-icon" aria-hidden>
          🎙
        </span>
        <div className="min-w-0 flex-1">
          <p className="se-setting-title">语音合成 TTS</p>
          <p className="text-[11px] text-[color:var(--se-dim)]">
            当前：{cfg.provider === "fish" ? "Fish Audio" : "edge（免费）"}
            {cfg.provider === "fish" && cfg.configured ? " · 已配置" : ""}
          </p>
        </div>
      </div>

      {/* provider 二选一 */}
      <div className="mt-3 grid grid-cols-2 gap-2">
        {(
          [
            { id: "edge" as const, name: "edge", desc: "免费 · 微软朗读" },
            { id: "fish" as const, name: "Fish Audio", desc: "S2.1 Pro · 有情绪" },
          ]
        ).map((p) => (
          <button
            key={p.id}
            type="button"
            onClick={() => setProvider(p.id)}
            aria-pressed={provider === p.id}
            className={`rounded-[8px] border px-2.5 py-2 text-left ${
              provider === p.id
                ? "border-[color:var(--se-accent)] bg-[color:var(--se-accent)]/8"
                : "border-[color:var(--se-line)]"
            }`}
          >
            <span className="block text-[12.5px] font-medium">{p.name}</span>
            <span className="block text-[10.5px] text-[color:var(--se-dim)]">{p.desc}</span>
          </button>
        ))}
      </div>

      {provider === "fish" ? (
        <div className="mt-3 space-y-2">
          <label className="block">
            <span className="mb-1 block text-[11.5px] text-[color:var(--se-dim)]">API Key</span>
            <input
              className={input}
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={cfg.fish_api_key || "sk-fish-..."}
              autoComplete="off"
            />
          </label>
          <div className="grid grid-cols-2 gap-2">
            <label className="block">
              <span className="mb-1 block text-[11.5px] text-[color:var(--se-dim)]">模型</span>
              <input className={input} value={model} onChange={(e) => setModel(e.target.value)} />
            </label>
            <label className="block">
              <span className="mb-1 block text-[11.5px] text-[color:var(--se-dim)]">格式</span>
              <select
                className={input}
                value={cfg.fish_format || "mp3"}
                onChange={(e) => setCfg((c) => ({ ...c, fish_format: e.target.value }))}
              >
                <option value="mp3">mp3</option>
                <option value="wav">wav</option>
              </select>
            </label>
          </div>
          <div className="block">
            <span className="mb-1 block text-[11.5px] text-[color:var(--se-dim)]">
              兜底音色（可选）
            </span>
            <span className="mb-1.5 block text-[10.5px] leading-[15px] text-[color:var(--se-dim)]">
              每个好友的声音在「偶遇/捏人」时<b>随机分配</b>，不用你挑。
              这里填的只在这个兜底场景用：好友没分到音色时。
            </span>
            <div className="flex gap-1.5">
              <input
                className={input}
                value={voiceQ}
                onChange={(e) => setVoiceQ(e.target.value)}
                placeholder="搜索音色名，如 British / Emma"
                onKeyDown={(e) => {
                  if (e.key === "Enter") void loadVoices(voiceQ, voiceLang);
                }}
              />
              <select
                className="rounded-[8px] border border-[color:var(--se-line)] bg-white px-1.5 text-[12px]"
                value={voiceLang}
                onChange={(e) => {
                  setVoiceLang(e.target.value);
                  void loadVoices(voiceQ, e.target.value);
                }}
              >
                <option value="en">英语</option>
                <option value="zh">中文</option>
                <option value="ja">日语</option>
                <option value="ko">韩语</option>
                <option value="fr">法语</option>
                <option value="de">德语</option>
                <option value="es">西语</option>
                <option value="pt">葡语</option>
                <option value="it">意语</option>
                <option value="">全部</option>
              </select>
              <button
                type="button"
                onClick={() => void loadVoices(voiceQ, voiceLang)}
                disabled={voicesBusy}
                className="shrink-0 rounded-[8px] border border-[color:var(--se-line)] px-2 text-[12px] disabled:opacity-50"
              >
                {voicesBusy ? "…" : "搜"}
              </button>
            </div>

            {voicesErr ? (
              <span className="mt-1 block text-[10.5px] text-[#fa5151]">{voicesErr}</span>
            ) : null}

            {voices.length > 0 ? (
              <div className="mt-1.5 max-h-[190px] overflow-y-auto rounded-[8px] border border-[color:var(--se-line)]">
                {voices.map((v) => {
                  const on = v.id === refId;
                  return (
                    <button
                      key={v.id}
                      type="button"
                      onClick={() => {
                        setRefId(v.id);
                        setPickedTitle(v.title);
                      }}
                      aria-pressed={on}
                      className={`block w-full border-b border-[color:var(--se-line)] px-2 py-1.5 text-left last:border-b-0 ${
                        on ? "bg-[color:var(--se-accent)]/10" : "hover:bg-black/[0.03]"
                      }`}
                    >
                      <span className="block truncate text-[12px] font-medium">{v.title}</span>
                      <span className="block truncate text-[10.5px] text-[color:var(--se-dim)]">
                        {(v.languages || []).join("/")}
                        {v.tags?.length ? ` · ${v.tags.slice(0, 3).join(", ")}` : ""}
                      </span>
                    </button>
                  );
                })}
              </div>
            ) : null}

            {refId ? (
              <button
                type="button"
                onClick={() => {
                  setRefId("");
                  setPickedTitle("");
                }}
                className="mt-1 text-[10.5px] text-[color:var(--se-dim)] underline"
              >
                清除兜底音色（好友自己的音色不受影响）
              </button>
            ) : null}
          </div>
        </div>
      ) : null}

      <div className="mt-3 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => void save()}
          disabled={busy !== null}
          className="rounded-[8px] bg-[color:var(--se-accent)] px-3 py-1.5 text-[12px] font-medium text-white disabled:opacity-50"
        >
          {busy === "save" ? "保存中…" : "保存"}
        </button>
        <button
          type="button"
          onClick={() => void test()}
          disabled={busy !== null}
          className="rounded-[8px] border border-[color:var(--se-line)] px-3 py-1.5 text-[12px] disabled:opacity-50"
        >
          {busy === "test" ? "合成中…" : "试听"}
        </button>
        <button
          type="button"
          onClick={() => void reset()}
          disabled={busy !== null}
          className="rounded-[8px] border border-[color:var(--se-line)] px-3 py-1.5 text-[12px] text-[color:var(--se-dim)] disabled:opacity-50"
        >
          恢复默认
        </button>
      </div>

      {msg ? <p className="mt-2 text-[11.5px] text-[color:var(--se-accent)]">{msg}</p> : null}
      {err ? (
        <p className="mt-2 whitespace-pre-wrap break-words text-[11px] text-[#fa5151]">{err}</p>
      ) : null}
      {audioUrl ? (
        <audio className="mt-2 w-full" controls autoPlay src={audioUrl} />
      ) : null}
    </section>
  );
}
