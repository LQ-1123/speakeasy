"use client";

/**
 * 我：自己的资料、练习统计、以及去设置/表达本的入口。
 * 参考微信「我」的排布：顶部身份卡 → 分组列表。
 */

import { useState } from "react";
import {
  BookMarked,
  ChevronRight,
  Headphones,
  Languages,
  LogOut,
  MessageCircle,
  Mic,
  Pencil,
  Settings,
  ShieldCheck,
  Sparkles,
  UsersRound,
} from "lucide-react";

import { LANGUAGES } from "../format";
import type { Route } from "../model";
import { saveProfile as apiSaveProfile } from "../apiFacade";
import { useApp } from "../store";
import { Avatar, Dialog, Icon } from "../ui";

export function MePane({ navigate }: { navigate: (r: Route) => void }) {
  const { profile, friends, expressions, updateProfile, settings, showToast } = useApp();
  const [editOpen, setEditOpen] = useState(false);

  const mastered = expressions.filter((e) => e.status === "mastered").length;
  const learning = expressions.length - mastered;
  const turns = friends.reduce((s, f) => s + f.turns, 0);

  return (
    <div className="w4-main">
      <div className="w4-head">
        <div className="w4-head-main">
          <div className="w4-head-title">我</div>
        </div>
      </div>

      <div className="w4-scroll">
        <div className="w4-me">
          <div className="w4-me-card">
            <Avatar src={profile.avatar} name={profile.name} size={80} round />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div className="w4-me-name">{profile.name}</div>
              <div className="w4-me-sub">
                {profile.city} · {profile.job} · {profile.level}
                {ageOf(profile.birthday) ? ` · ${ageOf(profile.birthday)} 岁` : ""}
              </div>
              <div className="w4-chips" style={{ marginTop: 7 }}>
                {profile.targets.map((t) => {
                  const l = LANGUAGES.find((x) => x.code === t);
                  return (
                    <span key={t} className="w4-chip is-green">
                      在练 {l?.label ?? t}
                    </span>
                  );
                })}
              </div>
              {profile.interests?.length ? (
                <div className="w4-chips" style={{ marginTop: 6 }}>
                  {profile.interests.slice(0, 8).map((t) => (
                    <span key={t} className="w4-chip">
                      {t}
                    </span>
                  ))}
                </div>
              ) : null}
            </div>
            <button className="w4-icon-btn" onClick={() => setEditOpen(true)} aria-label="编辑资料">
              <Pencil size={15} strokeWidth={1.8} />
            </button>
          </div>

          <div className="w4-stats">
            <button className="w4-stat" onClick={() => navigate({ name: "contacts" })}>
              <div className="w4-stat-num">{friends.length}</div>
              <div className="w4-stat-label">位语伴</div>
            </button>
            <button className="w4-stat" onClick={() => navigate({ name: "expressions" })}>
              <div className="w4-stat-num">{mastered}</div>
              <div className="w4-stat-label">条已掌握</div>
            </button>
            <button className="w4-stat" onClick={() => navigate({ name: "expressions" })}>
              <div className="w4-stat-num">{learning}</div>
              <div className="w4-stat-label">条在练</div>
            </button>
          </div>

          <div className="w4-section">
            <div className="w4-section-title">设置</div>
            <div className="w4-cells">
              <button className="w4-cell" onClick={() => navigate({ name: "settings", section: "llm" })}>
                <span className="w4-cell-icon">
                  <Sparkles size={15} strokeWidth={1.8} />
                </span>
                <span className="w4-cell-label">对话模型</span>
                <span className="w4-cell-value">{settings.llm.hasKey ? settings.llm.model : "演示模式"}</span>
                <ChevronRight size={15} className="w4-cell-chevron" />
              </button>
              <button className="w4-cell" onClick={() => navigate({ name: "settings", section: "voice" })}>
                <span className="w4-cell-icon">
                  <Headphones size={15} strokeWidth={1.8} />
                </span>
                <span className="w4-cell-label">声音</span>
                <span className="w4-cell-value">
                  {settings.tts.provider === "fish" ? "Fish Audio" : "系统朗读"}
                  {settings.tts.emotion ? " · 带情绪" : ""}
                </span>
                <ChevronRight size={15} className="w4-cell-chevron" />
              </button>
              <button className="w4-cell" onClick={() => navigate({ name: "settings", section: "stt" })}>
                <span className="w4-cell-icon">
                  <Mic size={15} strokeWidth={1.8} />
                </span>
                <span className="w4-cell-label">语音识别</span>
                <span className="w4-cell-value">{settings.stt.provider === "auto" ? "自动" : settings.stt.model}</span>
                <ChevronRight size={15} className="w4-cell-chevron" />
              </button>
              <button className="w4-cell" onClick={() => navigate({ name: "settings", section: "privacy" })}>
                <span className="w4-cell-icon">
                  <ShieldCheck size={15} strokeWidth={1.8} />
                </span>
                <span className="w4-cell-label">隐私与数据</span>
                <ChevronRight size={15} className="w4-cell-chevron" />
              </button>
              <button className="w4-cell" onClick={() => navigate({ name: "settings", section: "about" })}>
                <span className="w4-cell-icon">
                  <Languages size={15} strokeWidth={1.8} />
                </span>
                <span className="w4-cell-label">关于 SpeakEasy</span>
                <span className="w4-cell-value">2.4.0</span>
                <ChevronRight size={15} className="w4-cell-chevron" />
              </button>
            </div>
          </div>

          <div className="w4-me-foot">
            练习记录只存在这台设备上。
            <br />
            清掉浏览器数据，聊天和表达本都会一起消失。
          </div>
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
            });
            if (!ok) {
              showToast("资料没保存成功，请稍后再试");
              return false;
            }
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
  }) => Promise<boolean>;
}) {
  const { profile } = useApp();
  const [name, setName] = useState(profile.name);
  const [city, setCity] = useState(profile.city);
  const [job, setJob] = useState(profile.job);
  const [targets, setTargets] = useState<string[]>(profile.targets);
  const [level, setLevel] = useState(profile.level);
  const [birthday, setBirthday] = useState(profile.birthday ?? "");
  const [interests, setInterests] = useState<string[]>(profile.interests ?? []);
  const [interestInput, setInterestInput] = useState("");
  const [gender, setGender] = useState<"male" | "female" | "">(profile.gender ?? "");
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
      <div className="w4-field">
        <div className="w4-field-label">现在的水平</div>
        <div className="w4-chips">
          {(["刚开始", "能聊几句", "日常对话", "比较流利"] as const).map((lv) => (
            <button
              key={lv}
              className={`w4-chip ${level === lv ? "is-green" : ""}`}
              style={{ cursor: "pointer", padding: "4px 9px", fontSize: 12 }}
              onClick={() => setLevel(lv)}
            >
              {lv}
            </button>
          ))}
        </div>
        <div className="w4-field-hint">好友会按这个调整语速和用词，不会一上来就说太难的话。</div>
      </div>
      {saveError ? (
        <div className="w4-field-hint" style={{ color: "#c0392b" }}>
          {saveError}
        </div>
      ) : null}
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
