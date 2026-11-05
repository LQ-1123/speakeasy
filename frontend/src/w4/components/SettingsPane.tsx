"use client";

/**
 * 设置。分五组：对话模型 / 声音 / 语音识别 / 提醒 / 隐私与关于。
 * 每一组都只做真实存在的事，没有「敬请期待」。
 */

import { useEffect, useState } from "react";
import {
  CheckCircle2,
  ChevronLeft,
  CircleAlert,
  Copy,
  KeyRound,
  Mic,
  Play,
  Sparkles,
  Volume2,
} from "lucide-react";

import { saveLlm, saveStt, saveTts, ttsSampleUrl } from "../api";

import { isLocalMode, loadKeys, saveKeys } from "../local/keys";
import { exportLocalData, importLocalData, playVoiceUrl, synthesizeVoice, wipeLocalData } from "../apiFacade";
import type { Route } from "../model";
import { useApp, type Settings } from "../store";
import { Icon, Segmented, Switch } from "../ui";

const SECTIONS = [
  { key: "llm", label: "对话模型" },
  { key: "voice", label: "声音" },
  { key: "stt", label: "语音识别" },
  { key: "notify", label: "提醒" },
  { key: "privacy", label: "隐私与数据" },
  { key: "about", label: "关于" },
  { key: "local", label: "本地与密钥" },
] as const;

export function SettingsPane({
  section,
  navigate,
}: {
  section?: string;
  navigate: (r: Route) => void;
}) {
  const { settings, updateSettings, showToast } = useApp();
  const local = isLocalMode();
  const sections = local
    ? SECTIONS.filter((s) => s.key === "local" || s.key === "notify" || s.key === "privacy" || s.key === "about")
    : SECTIONS;
  const [active, setActive] = useState<string>(section ?? (local ? "local" : "llm"));
  const [playing, setPlaying] = useState(false);

  return (
    <div className="w4-main">
      <div className="w4-head">
        <button className="w4-icon-btn" onClick={() => navigate({ name: "me" })} aria-label="返回">
          <ChevronLeft size={18} />
        </button>
        <div className="w4-head-main">
          <div className="w4-head-title">设置</div>
        </div>
      </div>

      <div className="w4-settings-layout">
        <div className="w4-settings-nav">
          {sections.map((s) => (
            <button key={s.key} className={active === s.key ? "on" : ""} onClick={() => setActive(s.key)}>
              {s.label}
            </button>
          ))}
        </div>

        <div className="w4-settings-main">
          <div className="w4-settings-inner">
            {/* 手机上分组导航折叠成下拉 */}
            <div className="w4-only-mobile" style={{ marginBottom: 16 }}>
              <select className="w4-select" value={active} onChange={(e) => setActive(e.target.value)}>
                {sections.map((s) => (
                  <option key={s.key} value={s.key}>
                    {s.label}
                  </option>
                ))}
              </select>
            </div>

            {active === "llm" ? <LlmSection settings={settings} update={updateSettings} toast={showToast} /> : null}
            {active === "local" ? <LocalKeysSection toast={showToast} /> : null}
            {active === "voice" ? (
              <VoiceSection settings={settings} update={updateSettings} toast={showToast} playing={playing} setPlaying={setPlaying} />
            ) : null}
            {active === "stt" ? <SttSection settings={settings} update={updateSettings} toast={showToast} /> : null}
            {active === "notify" ? <NotifySection settings={settings} update={updateSettings} /> : null}
            {active === "privacy" ? <PrivacySection toast={showToast} /> : null}
            {active === "about" ? <AboutSection /> : null}
          </div>
        </div>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- 对话模型 */

const PRESETS = [
  { id: "deepseek", name: "DeepSeek", baseUrl: "https://api.deepseek.com/v1", model: "deepseek-flash", note: "便宜、中文好" },
  { id: "openai", name: "OpenAI", baseUrl: "https://api.openai.com/v1", model: "gpt-4o-mini", note: "最稳，略贵" },
  { id: "siliconflow", name: "硅基流动", baseUrl: "https://api.siliconflow.cn/v1", model: "Qwen/Qwen2.5-72B-Instruct", note: "国内直连" },
  { id: "custom", name: "自定义", baseUrl: "", model: "", note: "任何 OpenAI 兼容接口" },
];

function LlmSection({
  settings,
  update,
  toast,
}: {
  settings: Settings;
  update: (p: Partial<Settings>) => void;
  toast: (t: string, tone?: "success" | "error") => void;
}) {
  const [preset, setPreset] = useState(settings.llm.preset);
  const [baseUrl, setBaseUrl] = useState(settings.llm.baseUrl);
  const [model, setModel] = useState(settings.llm.model);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);

  /* store 里的设置是异步从后端拉回来的：到了要同步进输入框，
     否则页面永远停在 mock 的 deepseek-flash */
  useEffect(() => {
    setBaseUrl(settings.llm.baseUrl);
    setModel(settings.llm.model);
  }, [settings.llm.baseUrl, settings.llm.model]);

  const applyPreset = (id: string) => {
    setPreset(id);
    const p = PRESETS.find((x) => x.id === id);
    if (p && id !== "custom") {
      setBaseUrl(p.baseUrl);
      setModel(p.model);
    }
  };

  const save = async () => {
    setBusy(true);
    const res = await saveLlm({
      base_url: baseUrl.trim(),
      model: model.trim(),
      api_key: key.trim() || undefined,
    });
    setBusy(false);
    if (!res) {
      toast("保存失败：检查接口地址和 Key", "error");
      return;
    }
    const cfg = (res as Record<string, any>).config ?? {};
    update({
      llm: {
        preset,
        baseUrl: String(cfg.base_url ?? baseUrl),
        model: String(cfg.model ?? model),
        keyMasked: String(cfg.api_key_masked ?? settings.llm.keyMasked),
        hasKey: Boolean(cfg.api_key_set ?? settings.llm.hasKey),
      },
    });
    setKey("");
    toast("已保存，下一条消息就会用新的模型", "success");
  };

  return (
    <section>
      <SectionTitle title="对话模型" desc="好友的性格、记忆和回复都由它生成。" />

      <div className="w4-field">
        <div className="w4-field-label">用哪家</div>
        <div className="w4-provider-grid">
          {PRESETS.map((p) => (
            <button
              key={p.id}
              className={`w4-provider ${preset === p.id ? "on" : ""}`}
              onClick={() => applyPreset(p.id)}
            >
              <span className="w4-provider-name">{p.name}</span>
              <span className="w4-provider-note">{p.note}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="w4-field">
        <div className="w4-field-label">接口地址</div>
        <input className="w4-input" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://…/v1" />
      </div>
      <div className="w4-field">
        <div className="w4-field-label">模型名</div>
        <input className="w4-input" value={model} onChange={(e) => setModel(e.target.value)} placeholder="deepseek-flash" />
      </div>
      <div className="w4-field">
        <div className="w4-field-label">API Key</div>
        <input
          className="w4-input"
          type="password"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          placeholder={settings.llm.hasKey ? settings.llm.keyMasked : "粘贴你的 Key"}
          autoComplete="off"
        />
        <div className="w4-field-hint">
          <KeyRound size={11} style={{ verticalAlign: -1 }} /> Key 只存在这台设备上，接口不会回传明文。
        </div>
      </div>

      <div className="w4-save-row">
        <button className="w4-btn is-primary" onClick={save} disabled={busy}>
          {busy ? "正在验证…" : "保存并验证"}
        </button>
        <span className={`w4-status ${settings.llm.hasKey ? "ok" : "warn"}`}>
          {settings.llm.hasKey ? (
            <>
              <CheckCircle2 size={12} /> 已配置
            </>
          ) : (
            <>
              <CircleAlert size={12} /> 未配置，当前演示模式
            </>
          )}
        </span>
      </div>
    </section>
  );
}


let _waCtx: AudioContext | null = null;
async function playPreviewAudio(url: string): Promise<boolean> {
  try {
    const res = await fetch(url);
    if (!res.ok) return false;
    const buf = await res.arrayBuffer();
    _waCtx = _waCtx ?? new AudioContext();
    if (_waCtx.state === "suspended") await _waCtx.resume();
    const audio = await _waCtx.decodeAudioData(buf);
    const src = _waCtx.createBufferSource();
    src.buffer = audio;
    src.connect(_waCtx.destination);
    src.start();
    return true;
  } catch {
    return false;
  }
}

const PREVIEW_TEXT = "Morning! Right — the flat white guy. I remember you.";

/* ---------------------------------------------------------------- 声音 */

function VoiceSection({
  settings,
  update,
  toast,
  playing,
  setPlaying,
}: {
  settings: Settings;
  update: (p: Partial<Settings>) => void;
  toast: (t: string, tone?: "success" | "error") => void;
  playing: boolean;
  setPlaying: (v: boolean) => void;
}) {
  const [fishKey, setFishKey] = useState("");
  const [savingKey, setSavingKey] = useState(false);

  const saveKey = async () => {
    if (!fishKey.trim()) {
      toast("先把 Fish Audio 的 Key 粘进来", "error");
      return;
    }
    setSavingKey(true);
    const res = await saveTts({ api_key: fishKey.trim() });
    setSavingKey(false);
    if (!res) {
      toast("保存失败，Key 可能不对", "error");
      return;
    }
    update({ tts: { ...settings.tts, hasKey: true, keyMasked: "已保存" } });
    setFishKey("");
    toast("密钥已保存", "success");
  };

  return (
    <section>
      <SectionTitle title="声音" desc="好友说话用哪种合成方式，以及要不要带情绪。" />

      <div className="w4-field">
        <div className="w4-field-label">合成方式</div>
        <Segmented
          value={settings.tts.provider}
          options={[
            { value: "fish", label: "Fish Audio（自然）" },
            { value: "system", label: "系统朗读（免费）" },
          ]}
          onChange={(v) => update({ tts: { ...settings.tts, provider: v } })}
        />
        <div className="w4-field-hint">
          Fish Audio 会带语气和停顿，听起来像真人；系统朗读稳定但偏机械。
        </div>
      </div>

      {settings.tts.provider === "fish" ? (
        <>
          <div className="w4-field">
            <div className="w4-field-label">服务密钥</div>
            <input
              className="w4-input"
              type="password"
              value={fishKey}
              onChange={(e) => setFishKey(e.target.value)}
              placeholder={settings.tts.hasKey ? settings.tts.keyMasked : "粘贴 Fish Audio Key"}
            />
            <div className="w4-field-hint" style={{ marginTop: 8 }}>
              <button className="w4-btn" onClick={() => void saveKey()} disabled={savingKey}>
                {savingKey ? "保存中…" : "保存密钥"}
              </button>
            </div>
          </div>
          <div className="w4-settings-row">
            <div>
              <div className="w4-settings-row-title">带情绪</div>
              <div className="w4-settings-row-desc">好友会根据聊天内容调整语气，偶尔叹气、笑一下。</div>
            </div>
            <Switch
              on={settings.tts.emotion}
              onChange={(v) => update({ tts: { ...settings.tts, emotion: v } })}
            />
          </div>
        </>
      ) : null}

      <div className="w4-settings-row">
        <div>
          <div className="w4-settings-row-title">我的音色克隆</div>
          <div className="w4-settings-row-desc">
            {settings.tts.cloned
              ? "好友会用你的声音说外语。可以随时关掉。"
              : "上传 30 秒录音，让好友用你的声音说外语。"}
          </div>
        </div>
        <button
          className="w4-btn"
          onClick={() => {
            update({ tts: { ...settings.tts, cloned: !settings.tts.cloned } });
            toast(settings.tts.cloned ? "已关闭音色克隆" : "已开启（原型演示）", "success");
          }}
        >
          {settings.tts.cloned ? "已开启" : "去设置"}
        </button>
      </div>

      <div className="w4-field" style={{ marginTop: 18 }}>
        <div className="w4-field-label">试听</div>
        <div className="w4-try-card">
          <button
            className="w4-try-play"
            aria-label="试听"
            onClick={async () => {
              if (playing) {
                setPlaying(false);
                return;
              }
              setPlaying(true);
              const ok = await playPreviewAudio(ttsSampleUrl(PREVIEW_TEXT, "en-GB-RyanNeural"));
              if (!ok) toast("试听失败，请检查网络或声音配置", "error");
              setPlaying(false);
            }}
          >
            <Play size={14} fill="currentColor" strokeWidth={0} />
          </button>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="w4-try-text">“Morning! Right — the flat white guy. I remember you.”</div>
            <div className="w4-try-meta">{playing ? "正在播放 · Callum · 布里斯托" : "Callum · 布里斯托口音 · 0:04"}</div>
          </div>
          <Volume2 size={15} style={{ color: "var(--w-text-3)" }} />
        </div>
      </div>
    </section>
  );
}

/* ---------------------------------------------------------------- 语音识别 */

function SttSection({
  settings,
  update,
  toast,
}: {
  settings: Settings;
  update: (p: Partial<Settings>) => void;
  toast: (t: string, tone?: "success" | "error") => void;
}) {
  const [sttKey, setSttKey] = useState("");
  const [savingKey, setSavingKey] = useState(false);

  const saveKey = async () => {
    if (!sttKey.trim()) {
      toast("先粘贴云端识别的 Key", "error");
      return;
    }
    setSavingKey(true);
    const res = await saveStt({ api_key: sttKey.trim() });
    setSavingKey(false);
    if (!res) {
      toast("保存失败，Key 可能不对", "error");
      return;
    }
    update({ stt: { ...settings.stt, hasKey: true, keyMasked: "已保存" } });
    setSttKey("");
    toast("密钥已保存", "success");
  };

  return (
    <section>
      <SectionTitle title="语音识别" desc="你按住说话时，把声音转成文字用哪套服务。" />
      <div className="w4-field">
        <div className="w4-field-label">识别方式</div>
        <Segmented
          value={settings.stt.provider}
          options={[
            { value: "auto", label: "自动" },
            { value: "local", label: "本地" },
            { value: "siliconflow", label: "云端" },
          ]}
          onChange={(v) => update({ stt: { ...settings.stt, provider: v } })}
        />
        <div className="w4-field-hint">
          自动：能用云端就用云端（更快），失败时回退到本地。本地模式不联网，稍慢一点。
        </div>
      </div>

      {settings.stt.provider !== "local" ? (
        <div className="w4-field">
          <div className="w4-field-label">云端密钥</div>
          <input
            className="w4-input"
            type="password"
            value={sttKey}
            onChange={(e) => setSttKey(e.target.value)}
            placeholder={settings.stt.hasKey ? settings.stt.keyMasked : "硅基流动的 API Key"}
          />
          <div className="w4-field-hint">用 SenseVoiceSmall，中文和几种小语种都识别得不错。</div>
          <div className="w4-field-hint" style={{ marginTop: 8 }}>
            <button className="w4-btn" onClick={() => void saveKey()} disabled={savingKey}>
              {savingKey ? "保存中…" : "保存密钥"}
            </button>
          </div>
        </div>
      ) : null}

      <div className="w4-field">
        <div className="w4-field-label">识别语言</div>
        <select className="w4-select">
          <option>自动判断</option>
          <option>英语</option>
          <option>日语</option>
          <option>韩语</option>
          <option>西班牙语</option>
        </select>
      </div>

      <div className="w4-save-row">
        <button className="w4-btn is-primary" onClick={() => toast("设置已保存", "success")}>
          保存
        </button>
        <button className="w4-btn" onClick={() => toast("开始录音后说一句话，就能看到识别结果", "success")}>
          <Mic size={14} />
          录一句试试
        </button>
      </div>
    </section>
  );
}

/* ---------------------------------------------------------------- 提醒 */

function NotifySection({
  settings,
  update,
}: {
  settings: Settings;
  update: (p: Partial<Settings>) => void;
}) {
  return (
    <section>
      <SectionTitle title="提醒" desc="只在你允许的范围内推送。" />
      <div className="w4-settings-row">
        <div>
          <div className="w4-settings-row-title">好友发来新语音</div>
          <div className="w4-settings-row-desc">离开页面时，好友的回复会推送到系统通知。</div>
        </div>
        <Switch
          on={settings.notify.messages}
          onChange={(v) => update({ notify: { ...settings.notify, messages: v } })}
        />
      </div>
      <div className="w4-settings-row">
        <div>
          <div className="w4-settings-row-title">该练表达了</div>
          <div className="w4-settings-row-desc">每天固定时间提醒你复习这几条，不再频繁打扰。</div>
        </div>
        <Switch
          on={settings.notify.practice}
          onChange={(v) => update({ notify: { ...settings.notify, practice: v } })}
        />
      </div>
    </section>
  );
}

/* ---------------------------------------------------------------- 隐私 */

function PrivacySection({ toast }: { toast: (t: string, t2?: "success") => void }) {
  return (
    <section>
      <SectionTitle title="隐私与数据" desc="这里做过的所有事，都只存这台设备。" />
      <div className="w4-settings-row">
        <div>
          <div className="w4-settings-row-title">聊天记录</div>
          <div className="w4-settings-row-desc">包含语音原文、译文和好友记住的事情。</div>
        </div>
        <button className="w4-btn is-danger" onClick={() => toast("原型里不会真的删除", "success")}>
          全部清除
        </button>
      </div>
      <div className="w4-settings-row">
        <div>
          <div className="w4-settings-row-title">导出数据</div>
          <div className="w4-settings-row-desc">把聊天、表达本导出成一个文件，随时带走。</div>
        </div>
        <button className="w4-btn" onClick={() => toast("已生成下载（原型演示）", "success")}>
          导出
        </button>
      </div>
      <div className="w4-settings-row">
        <div>
          <div className="w4-settings-row-title">好友能记住什么</div>
          <div className="w4-settings-row-desc">只记你主动说过的信息，不收集通讯录、位置和相册。</div>
        </div>
        <button className="w4-btn" onClick={() => toast("所有记忆条目都可以逐条删除", "success")}>
          查看
        </button>
      </div>
    </section>
  );
}

function AboutSection() {
  return (
    <section>
      <SectionTitle title="关于 SpeakEasy" desc="一款用来把「不敢开口」变成「顺口说出来」的工具。" />
      <div className="w4-about">
        <div className="w4-about-row">
          <span>版本</span>
          <span>2.4.0（原型）</span>
        </div>
        <div className="w4-about-row">
          <span>创作者</span>
          <span>SpeakEasy 团队</span>
        </div>
        <div className="w4-about-row">
          <span>数据</span>
          <span>全部本地</span>
        </div>
        <div className="w4-about-row">
          <span>头像素材</span>
          <span>Personas by Draftbit（CC BY 4.0）· DiceBear 生成</span>
        </div>
      </div>
      <p className="w4-about-text">
        你缺的不是词汇量，是在真实压力下用另一种语言反应的条件反射。
        所以这里没有评分、没有打卡、没有排行榜——只有一群会等你把话说完的语伴。
      </p>
    </section>
  );
}

/* ---------------------------------------------------------------- 小组件 */

function SectionTitle({ title, desc }: { title: string; desc: string }) {
  return (
    <div style={{ marginBottom: 18 }}>
      <div style={{ fontSize: 16, fontWeight: 600 }}>{title}</div>
      <div style={{ fontSize: 12.5, color: "var(--w-text-3)", marginTop: 3 }}>{desc}</div>
    </div>
  );
}

/** 本地模式：所有云端 key 只保存在本机，不经过任何服务器 */
function LocalKeysSection({ toast }: { toast: (m: string, tone?: "success" | "error" | "default") => void }) {
  const [keys, setKeys] = useState(() => loadKeys());
  const bind = (field: keyof typeof keys) => ({
    value: keys[field],
    onChange: (e: { target: { value: string } }) => setKeys({ ...keys, [field]: e.target.value }),
  });
  return (
    <div className="w4-settings-section">
      <div className="w4-field-label">DeepSeek Key（对话）</div>
      <input className="w4-input" {...bind("llmKey")} placeholder="sk-..." />
      <div className="w4-field-label" style={{ marginTop: 12 }}>Fish Audio Key（在线音色库，App 内可用）</div>
      <input className="w4-input" {...bind("fishKey")} placeholder="sk-fish-..." />
      <div className="w4-field-label" style={{ marginTop: 12 }}>Fish 音色 Reference ID（可选）</div>
      <input className="w4-input" {...bind("fishRef")} placeholder="留空则用默认音色" />
      <div className="w4-field-label" style={{ marginTop: 12 }}>SiliconFlow Key（语音识别 + 语音合成）</div>
      <input className="w4-input" {...bind("sttKey")} placeholder="sk-..." />
      <button
        className="w4-btn is-primary"
        style={{ marginTop: 16 }}
        onClick={() => {
          saveKeys(keys);
          toast("已保存到本机");
        }}
      >
        保存到本机
      </button>
      <button
        className="w4-btn"
        style={{ marginTop: 10, marginLeft: 8 }}
        onClick={() =>
          void (async () => {
            toast("正在合成测试语音…");
            const url = await synthesizeVoice("Hello! This is a voice test. Can you hear me clearly?");
            if (!url) {
              toast("合成失败：检查 SiliconFlow Key 和网络", "error");
              return;
            }
            const ok = await playVoiceUrl(url);
            toast(ok ? "开始播放测试语音" : "播放失败：手机音频权限/静音检查", ok ? "success" : "error");
          })()
        }
      >
        测试声音
      </button>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 14 }}>
        <button
          className="w4-btn"
          onClick={() =>
            void (async () => {
              const data = await exportLocalData();
              if (!data) return toast("导出失败", "error");
              const blob = new Blob([data], { type: "application/json" });
              const a = document.createElement("a");
              a.href = URL.createObjectURL(blob);
              a.download = `speakeasy-backup-${new Date().toISOString().slice(0, 10)}.json`;
              a.click();
              URL.revokeObjectURL(a.href);
              toast("备份已导出");
            })()
          }
        >
          导出数据
        </button>
        <label className="w4-btn" style={{ cursor: "pointer" }}>
          导入数据
          <input
            type="file"
            accept="application/json,.json"
            style={{ display: "none" }}
            onChange={(e) => {
              const file = e.target.files && e.target.files[0];
              if (!file) return;
              void (async () => {
                const text = await file.text();
                const ok = await importLocalData(text);
                toast(ok ? "导入成功，正在刷新…" : "导入失败：不是有效的备份文件", ok ? "success" : "error");
                if (ok) window.setTimeout(() => window.location.reload(), 800);
              })();
            }}
          />
        </label>
        <button
          className="w4-btn"
          onClick={() =>
            void (async () => {
              if (!window.confirm("清空本机所有好友、聊天、动态？此操作不可撤销。")) return;
              await wipeLocalData();
              toast("已清空，正在刷新…");
              window.setTimeout(() => window.location.reload(), 800);
            })()
          }
        >
          清空数据
        </button>
      </div>
      <p className="w4-hint" style={{ marginTop: 10 }}>
        Key 只存在这台手机上，用于直接调用云端接口；不会上传到任何服务器。
        数据也只在本机，换手机时用「导出数据」备份、新手机「导入数据」。
      </p>
    </div>
  );
}
