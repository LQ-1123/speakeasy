"use client";

/**
 * 设置。只保留 提醒 / 隐私与数据 / 关于。
 *
 * 模型、密钥、语音识别/合成配置已全部收进本机管理端（/admin），客户端不再提供
 * 任何配置入口，避免零基础客户误改后整个应用不可用。
 */

import {
  useState,
} from "react";
import {
  ChevronLeft,
} from "lucide-react";


import { playMessageSound, playRequestSound } from "../notify";

import type { Route } from "../model";
import {
  useApp,
  type Settings,
} from "../store";
import {
  Switch,
} from "../ui";

const SECTIONS = [
  { key: "notify", label: "提醒" },
  { key: "privacy", label: "隐私与数据" },
  { key: "about", label: "关于" },
] as const;

export function SettingsPane({
  section,
  navigate,
}: {
  section?: string;
  navigate: (r: Route) => void;
}) {
  const { settings, updateSettings, showToast } = useApp();
  const sections = SECTIONS;
  const initialSection = section && SECTIONS.some((s) => s.key === section)
    ? section
    : "notify";
  const [active, setActive] = useState<string>(initialSection);

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

            {active === "notify" ? <NotifySection settings={settings} update={updateSettings} /> : null}
            {active === "privacy" ? <PrivacySection toast={showToast} /> : null}
            {active === "about" ? <AboutSection /> : null}
          </div>
        </div>
      </div>
    </div>
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
      <div className="w4-settings-row">
        <div>
          <div className="w4-settings-row-title">提示音</div>
          <div className="w4-settings-row-desc">试听新消息和新好友申请的提示音。</div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="w4-btn" onClick={() => playMessageSound()}>消息</button>
          <button className="w4-btn" onClick={() => playRequestSound()}>新好友</button>
        </div>
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
        <button className="w4-btn is-danger" onClick={() => toast("当前版本暂不支持清除", "success")}>
          全部清除
        </button>
      </div>
      <div className="w4-settings-row">
        <div>
          <div className="w4-settings-row-title">导出数据</div>
          <div className="w4-settings-row-desc">把聊天、表达本导出成一个文件，随时带走。</div>
        </div>
        <button className="w4-btn" onClick={() => toast("当前版本暂不支持导出", "success")}>
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
          <span>2.4.0</span>
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
