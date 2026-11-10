"use client";

import { useShallow } from "zustand/react/shallow";

/**
 * 我：自己的资料、练习入口和设置。
 * 顶部是一张安静的身份卡（头像 / 名字 / 在练语言 / 一个编辑入口），
 * 下面按「练习 / 语伴 / 设置」分组排成列表，不用大数字统计面板。
 */

import { useState } from "react";
import {
  Bell,
  BookMarked,
  ChevronRight,
  Info,
  Palette,
  Pencil,
  ShieldCheck,
  Users,
} from "lucide-react";

import { haptic } from "../../theme/haptics";
import { LANGUAGES } from "../format";
import type { Route } from "../model";
import { saveProfile as apiSaveProfile } from "../apiFacade";
import { useApp } from "../store";
import { Avatar, Dialog } from "../ui";
import "../styles/profile.css";

/** 母语候选：中文 + 现有语言（LANGUAGES 只有在练语言，没有 zh） */
const NATIVE_LANGUAGES: Array<{ code: string; label: string; flag: string }> = [
  { code: "zh", label: "中文", flag: "🇨🇳" },
  ...LANGUAGES,
];

export function MePane({ navigate }: { navigate: (r: Route) => void }) {
  const { profile, friends, requests, expressions, updateProfile, showToast } = useApp(
    useShallow((state) => ({
      profile: state.profile,
      friends: state.friends,
      requests: state.requests,
      expressions: state.expressions,
      updateProfile: state.updateProfile,
      showToast: state.showToast,
    })),
  );
  const [editOpen, setEditOpen] = useState(false);

  const mastered = expressions.filter((e) => e.status === "mastered").length;
  const learning = expressions.length - mastered;
  const age = ageOf(profile.birthday);

  const metaParts = [profile.city, profile.job, age ? `${age} 岁` : ""].filter(Boolean);
  const interests = profile.interests ?? [];

  return (
    <div className="w4-main">
      <div className="w4-head">
        <div className="w4-head-main">
          <div className="w4-head-title">我</div>
        </div>
      </div>

      <div className="w4-scroll">
        <div className="w4-me">
          <header className="w4-me-hero">
            <div className="w4-me-hero-top">
              <span className="w4-me-avatar">
                <Avatar src={profile.avatar} name={profile.name} size={88} round />
              </span>
              <div className="w4-me-identity">
                <h1 className="w4-me-name">{profile.name}</h1>
                {metaParts.length ? <p className="w4-me-meta">{metaParts.join(" · ")}</p> : null}
                <div className="w4-me-chips">
                  {profile.targets.map((code) => {
                    const language = LANGUAGES.find((item) => item.code === code);
                    const level = profile.levels?.[code] ?? profile.level;
                    return (
                      <span key={code} className="w4-me-chip is-target">
                        {language ? `${language.flag} ${language.label}` : code}
                        {level ? ` · ${level}` : ""}
                      </span>
                    );
                  })}
                </div>
              </div>
            </div>

            <div className="w4-me-hero-foot">
              <div className="w4-me-interests">
                {interests.slice(0, 5).map((tag) => (
                  <span key={tag} className="w4-me-chip is-soft">
                    {tag}
                  </span>
                ))}
                {interests.length > 5 ? (
                  <span className="w4-me-chip is-soft">+{interests.length - 5}</span>
                ) : null}
              </div>
              <button
                className="w4-btn is-tonal w4-me-edit"
                onClick={() => {
                  haptic("selection");
                  setEditOpen(true);
                }}
              >
                <Pencil size={15} strokeWidth={1.9} />
                编辑资料
              </button>
            </div>
          </header>

          <section className="w4-me-group" aria-label="练习">
            <h2 className="w4-me-group-title">练习</h2>
            <div className="w4-me-card">
              <button className="w4-me-row" onClick={() => navigate({ name: "expressions" })}>
                <span className="w4-me-row-icon">
                  <BookMarked size={18} strokeWidth={1.8} />
                </span>
                <span className="w4-me-row-label">表达本</span>
                <span className="w4-me-row-value">
                  在练 {learning} · 掌握 {mastered}
                </span>
                <ChevronRight size={16} className="w4-me-row-chevron" />
              </button>
            </div>
          </section>

          <section className="w4-me-group" aria-label="语伴">
            <h2 className="w4-me-group-title">语伴</h2>
            <div className="w4-me-card">
              <button className="w4-me-row" onClick={() => navigate({ name: "contacts" })}>
                <span className="w4-me-row-icon">
                  <Users size={18} strokeWidth={1.8} />
                </span>
                <span className="w4-me-row-label">我的语伴</span>
                <span className="w4-me-row-value">{friends.length} 位</span>
                <ChevronRight size={16} className="w4-me-row-chevron" />
              </button>
              <button className="w4-me-row" onClick={() => navigate({ name: "requests" })}>
                <span className="w4-me-row-icon">
                  <Bell size={18} strokeWidth={1.8} />
                </span>
                <span className="w4-me-row-label">好友申请</span>
                <span className="w4-me-row-value">
                  {requests.length ? `${requests.length} 条待处理` : "暂无"}
                </span>
                <ChevronRight size={16} className="w4-me-row-chevron" />
              </button>
            </div>
          </section>

          <section className="w4-me-group" aria-label="设置">
            <h2 className="w4-me-group-title">设置</h2>
            <div className="w4-me-card">
              <button
                className="w4-me-row"
                onClick={() => navigate({ name: "settings", section: "notify" })}
              >
                <span className="w4-me-row-icon">
                  <Bell size={18} strokeWidth={1.8} />
                </span>
                <span className="w4-me-row-label">提醒</span>
                <ChevronRight size={16} className="w4-me-row-chevron" />
              </button>
              <button
                className="w4-me-row"
                onClick={() => navigate({ name: "settings", section: "appearance" })}
              >
                <span className="w4-me-row-icon">
                  <Palette size={18} strokeWidth={1.8} />
                </span>
                <span className="w4-me-row-label">外观与触感</span>
                <ChevronRight size={16} className="w4-me-row-chevron" />
              </button>
              <button
                className="w4-me-row"
                onClick={() => navigate({ name: "settings", section: "privacy" })}
              >
                <span className="w4-me-row-icon">
                  <ShieldCheck size={18} strokeWidth={1.8} />
                </span>
                <span className="w4-me-row-label">隐私与数据</span>
                <ChevronRight size={16} className="w4-me-row-chevron" />
              </button>
              <button
                className="w4-me-row"
                onClick={() => navigate({ name: "settings", section: "about" })}
              >
                <span className="w4-me-row-icon">
                  <Info size={18} strokeWidth={1.8} />
                </span>
                <span className="w4-me-row-label">关于 Sonder</span>
                <span className="w4-me-row-value">4.1.2</span>
                <ChevronRight size={16} className="w4-me-row-chevron" />
              </button>
            </div>
          </section>

          <p className="w4-me-foot-note">
            <ShieldCheck size={13} strokeWidth={1.8} />
            资料、聊天和表达都保存在你自己的账户里。
          </p>
        </div>
      </div>

      {editOpen ? (
        <EditProfileDialog
          onClose={() => setEditOpen(false)}
          onSave={async (patch) => {
            const ok = await apiSaveProfile(patch.name, patch.targets, patch.birthday, {
              city: patch.city,
              job: patch.job,
              interests: patch.interests,
              gender: patch.gender,
              nativeLanguage: patch.nativeLanguage,
              level: patch.levels[patch.targets[0]] || "能聊几句",
              levels: patch.levels,
            });
            if (!ok) {
              haptic("error");
              showToast("资料没保存成功，请稍后再试");
              return false;
            }
            haptic("save");
            updateProfile(patch);
            showToast("资料已保存");
            /* 保存成功后再刷新，确保后端数据已落库 */
            window.setTimeout(() => window.location.reload(), 400);
            return true;
          }}
        />
      ) : null}
    </div>
  );
}

function EditProfileDialog({
  onClose,
  onSave,
}: {
  onClose: () => void;
  onSave: (patch: {
    name: string;
    city: string;
    job: string;
    targets: string[];
    level: "刚开始" | "能聊几句" | "日常对话" | "比较流利";
    birthday: string;
    interests: string[];
    gender: "male" | "female" | "";
    nativeLanguage: string;
    levels: Record<string, string>;
  }) => Promise<boolean>;
}) {
  const { profile } = useApp(useShallow((state) => ({ profile: state.profile })));
  const [name, setName] = useState(profile.name);
  const [city, setCity] = useState(profile.city);
  const [job, setJob] = useState(profile.job);
  const [targets, setTargets] = useState<string[]>(profile.targets);
  const [level, setLevel] = useState(profile.level);
  const [birthday, setBirthday] = useState(profile.birthday ?? "");
  const [interests, setInterests] = useState<string[]>(profile.interests ?? []);
  const [interestInput, setInterestInput] = useState("");
  const [gender, setGender] = useState<"male" | "female" | "">(profile.gender ?? "");
  const [nativeLanguage, setNativeLanguage] = useState(profile.nativeLanguage ?? "zh");
  const [levels, setLevels] = useState<Record<string, string>>(profile.levels ?? {});
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");

  return (
    <Dialog
      title="编辑资料"
      onClose={onClose}
      footer={
        <>
          <button className="w4-btn" onClick={onClose}>
            取消
          </button>
          <button
            className="w4-btn is-primary"
            disabled={saving}
            onClick={async () => {
              setSaving(true);
              setSaveError("");
              try {
                const ok = await onSave({
                  name: name.trim() || profile.name,
                  city,
                  job,
                  targets,
                  level,
                  birthday,
                  interests,
                  gender,
                  nativeLanguage,
                  levels,
                });
                if (!ok) setSaveError("保存失败，请重试");
              } catch {
                setSaveError("保存失败，请检查网络");
              } finally {
                setSaving(false);
              }
            }}
          >
            {saving ? "保存中…" : "保存"}
          </button>
        </>
      }
    >
      <div className="w4-field">
        <div className="w4-field-label">性别（用于生成头像）</div>
        <div className="w4-chips">
          {(
            [
              ["female", "女"],
              ["male", "男"],
              ["", "不设置"],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value || "none"}
              className={`w4-chip ${gender === value ? "is-green" : ""}`}
              style={{ cursor: "pointer" }}
              onClick={() => setGender(value)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
      <div className="w4-field">
        <div className="w4-field-label">名字</div>
        <input className="w4-input" value={name} onChange={(e) => setName(e.target.value)} />
      </div>
      <div className="w4-field-row">
        <div className="w4-field">
          <div className="w4-field-label">城市</div>
          <input className="w4-input" value={city} onChange={(e) => setCity(e.target.value)} />
        </div>
        <div className="w4-field">
          <div className="w4-field-label">在做什么</div>
          <input className="w4-input" value={job} onChange={(e) => setJob(e.target.value)} />
        </div>
      </div>
      <div className="w4-field">
        <div className="w4-field-label">兴趣标签（影响偶遇与人物匹配）</div>
        <div className="w4-chips">
          {["咖啡", "美食", "旅行", "音乐", "读书", "电影", "摄影", "运动", "科技", "艺术"].map((t) => (
            <button
              key={t}
              className={`w4-chip ${interests.includes(t) ? "is-green" : ""}`}
              style={{ cursor: "pointer" }}
              onClick={() =>
                setInterests((list) =>
                  list.includes(t)
                    ? list.filter((x) => x !== t)
                    : list.length >= 8
                      ? list
                      : [...list, t],
                )
              }
            >
              {t}
            </button>
          ))}
          {interests
            .filter((t) => !["咖啡", "美食", "旅行", "音乐", "读书", "电影", "摄影", "运动", "科技", "艺术"].includes(t))
            .map((t) => (
              <button
                key={`custom-${t}`}
                className="w4-chip is-green"
                style={{ cursor: "pointer" }}
                title="点击删除"
                onClick={() => setInterests((list) => list.filter((x) => x !== t))}
              >
                {t} ×
              </button>
            ))}
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
          <input
            className="w4-input"
            style={{ flex: 1 }}
            placeholder="自定义标签（最多 8 个）"
            value={interestInput}
            maxLength={16}
            onChange={(e) => setInterestInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key !== "Enter") return;
              e.preventDefault();
              const tag = interestInput.trim().replace(/^#+/, "").slice(0, 16);
              if (!tag) return;
              setInterests((list) =>
                list.includes(tag) || list.length >= 8 ? list : [...list, tag],
              );
              setInterestInput("");
            }}
          />
        </div>
      </div>
      <div className="w4-field">
        <div className="w4-field-label">生日</div>
        <input
          className="w4-input"
          type="date"
          value={birthday}
          max="2020-12-31"
          onChange={(e) => setBirthday(e.target.value)}
        />
        <div className="w4-field-hint">填了之后，偶遇和自动匹配会优先找年龄相仿的人</div>
      </div>
      <div className="w4-field">
        <div className="w4-field-label">母语（用于解释/翻译）</div>
        <div className="w4-chips">
          {NATIVE_LANGUAGES.map((l) => (
            <button
              key={`native-${l.code}`}
              className={`w4-chip ${nativeLanguage === l.code ? "is-green" : ""}`}
              style={{ cursor: "pointer", padding: "4px 9px", fontSize: 12 }}
              onClick={() => setNativeLanguage(l.code)}
            >
              {l.flag} {l.label}
            </button>
          ))}
        </div>
      </div>
      <div className="w4-field">
        <div className="w4-field-label">在练</div>
        <div className="w4-chips">
          {LANGUAGES.map((l) => {
            const on = targets.includes(l.code);
            return (
              <button
                key={l.code}
                className={`w4-chip ${on ? "is-green" : ""}`}
                style={{ cursor: "pointer", padding: "4px 9px", fontSize: 12 }}
                onClick={() =>
                  setTargets((t) => (on ? (t.length > 1 ? t.filter((x) => x !== l.code) : t) : [...t, l.code]))
                }
              >
                {l.flag} {l.label}
              </button>
            );
          })}
        </div>
        <div className="w4-field-hint">可以多选。偶遇和好友回复会优先用这些语言。</div>
      </div>
      {targets.map((code) => {
        const lang = LANGUAGES.find((x) => x.code === code);
        const value = levels[code] ?? "能聊几句";
        return (
          <div className="w4-field" key={`level-${code}`}>
            <div className="w4-field-label">
              {lang ? `${lang.flag} ${lang.label}` : code} 的水平
            </div>
            <div className="w4-chips">
              {(["刚开始", "能聊几句", "日常对话", "比较流利"] as const).map((lv) => (
                <button
                  key={lv}
                  className={`w4-chip ${value === lv ? "is-green" : ""}`}
                  style={{ cursor: "pointer", padding: "4px 9px", fontSize: 12 }}
                  onClick={() => setLevels((m) => ({ ...m, [code]: lv }))}
                >
                  {lv}
                </button>
              ))}
            </div>
            <div className="w4-field-hint">
              该语言写多难由这个水平决定（越基础，句子越短、词越常用）。
            </div>
          </div>
        );
      })}
      {saveError ? <div className="w4-field-error">{saveError}</div> : null}
    </Dialog>
  );
}

/** 生日 → 年龄（没填/非法返回 null） */
function ageOf(birthday?: string): number | null {
  if (!birthday) return null;
  const d = new Date(birthday);
  if (Number.isNaN(d.getTime())) return null;
  const now = new Date();
  let age = now.getFullYear() - d.getFullYear();
  const m = now.getMonth() - d.getMonth();
  if (m < 0 || (m === 0 && now.getDate() < d.getDate())) age -= 1;
  return age > 0 && age < 120 ? age : null;
}
