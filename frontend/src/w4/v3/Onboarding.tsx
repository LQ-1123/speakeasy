"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft, ArrowRight, Check, Loader2, Sparkles } from "lucide-react";
import { useShallow } from "zustand/react/shallow";

import { haptic } from "../../theme/haptics";
import { saveProfileSetup, type ProfileSetupPayload } from "../apiFacade";
import { LANGUAGES } from "../format";
import type { Profile } from "../model";
import { useApp } from "../store";
import "../styles/onboarding.css";

/**
 * 首次进入的三步 Onboarding。
 *
 * 设计约束（来自产品）：
 *  - 只在最后一步点「完成」时提交一次 PUT（complete_setup: true），中途不写库；
 *  - 保存失败停在当前步骤，可重试；绝不出现「保存失败却显示完成」；
 *  - 后端 400 profile_incomplete 时按 missing 跳回缺字段的步骤并高亮；
 *  - 判断用户是否需要引导只看后端 profile_setup_completed_at（绝不用 localStorage）。
 *
 * 下面的校验/payload 构造函数都是纯函数并导出，单测直接引用，不依赖 DOM。
 */

/* ========================================================================== */
/* 常量                                                                       */
/* ========================================================================== */

export type CommunicationPreference = "natural" | "occasional" | "improve";
export type Level = "刚开始" | "能聊几句" | "日常对话" | "比较流利";
export type OnboardingStep = 1 | 2 | 3;

export const DEFAULT_LEVEL: Level = "能聊几句";
export const LEVEL_OPTIONS: Level[] = ["刚开始", "能聊几句", "日常对话", "比较流利"];
export const MAX_TARGET_LANGUAGES = 3;
export const MAX_INTERESTS = 8;
export const MAX_NAME_LENGTH = 40;
export const MAX_INTEREST_LENGTH = 16;

/**
 * 不是「真昵称」的占位值：后端默认昵称是 Traveler，旧前端兜底是「我」。
 * 预填时视为空，让用户自己填，避免带着默认名提交（后端也把 Traveler 当未填）。
 */
const PLACEHOLDER_NAMES = new Set(["我", "Traveler", "traveler"]);

/** 母语候选：值用语言代码（与后端 native_language 对齐），展示用各自母语写法。 */
export const NATIVE_LANGUAGE_OPTIONS: Array<{ code: string; label: string }> = [
  { code: "zh", label: "中文" },
  { code: "en", label: "English" },
  { code: "ja", label: "日本語" },
  { code: "ko", label: "한국어" },
  { code: "es", label: "Español" },
  { code: "de", label: "Deutsch" },
  { code: "fr", label: "Français" },
  { code: "pt", label: "Português" },
];

/** 12 个预设兴趣：值用英文（与 fact_sheet.interests 的既有数据一致），展示中文。 */
export const INTEREST_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "Technology", label: "科技" },
  { value: "Gaming", label: "游戏" },
  { value: "Music", label: "音乐" },
  { value: "Movies", label: "电影" },
  { value: "Travel", label: "旅行" },
  { value: "Photography", label: "摄影" },
  { value: "Coffee", label: "咖啡" },
  { value: "Food", label: "美食" },
  { value: "Sports", label: "运动" },
  { value: "Art", label: "艺术" },
  { value: "Books", label: "阅读" },
  { value: "Programming", label: "编程" },
];

/** 语伴怎么帮你：与后端 communication_preference 枚举一一对应。 */
export const COMMUNICATION_OPTIONS: Array<{
  value: CommunicationPreference;
  title: string;
  hint: string;
}> = [
  {
    value: "natural",
    title: "Just chat naturally. No corrections.",
    hint: "像朋友一样自然聊天，不打断我",
  },
  {
    value: "occasional",
    title: "Correct me occasionally.",
    hint: "偶尔纠正明显的错误",
  },
  {
    value: "improve",
    title: "Help me improve my expressions.",
    hint: "帮我把说法改得更地道",
  },
];

/** 三步的标题文案（第一步是 "Get to know you"）。 */
export const STEP_META: Record<OnboardingStep, { title: string; subtitle: string }> = {
  1: {
    title: "Get to know you",
    subtitle: "先认识一下你，之后聊天会更有共同话题。",
  },
  2: {
    title: "Your language journey",
    subtitle: "想练哪些语言？每门语言可以设定自己的水平。",
  },
  3: {
    title: "Things you love",
    subtitle: "选几个喜欢的话题，语伴会从这些开始聊。",
  },
};

/** 后端 missing 字段名 → 它属于哪一步（400 profile_incomplete 跳转用）。 */
const MISSING_FIELD_STEP: Record<string, OnboardingStep> = {
  nickname: 1,
  native_language: 1,
  target_languages: 2,
  levels: 2,
  interests: 3,
  communication_preference: 3,
};

/* ========================================================================== */
/* 纯逻辑（单测直接引用）                                                      */
/* ========================================================================== */

/** 编辑中的草稿；提交前不会写库。 */
export interface OnboardingDraft {
  name: string;
  nativeLanguage: string;
  targets: string[];
  levels: Record<string, string>;
  interests: string[];
  communicationPreference: CommunicationPreference;
}

export function isCommunicationPreference(value: unknown): value is CommunicationPreference {
  return value === "natural" || value === "occasional" || value === "improve";
}

/** 用已有 profile 预填草稿（昵称里的 "我" 是后端缺省值，不算真昵称）。 */
export function createDraft(profile: Profile): OnboardingDraft {
  const nativeLanguage = NATIVE_LANGUAGE_OPTIONS.some((item) => item.code === profile.nativeLanguage)
    ? String(profile.nativeLanguage)
    : "zh";
  const targets = (profile.targets ?? [])
    .filter(
      (code, index, list) =>
        code !== nativeLanguage &&
        list.indexOf(code) === index &&
        LANGUAGES.some((item) => item.code === code),
    )
    .slice(0, MAX_TARGET_LANGUAGES);
  const levels: Record<string, string> = {};
  for (const code of targets) levels[code] = levelFor(profile.levels, code);
  const interests = (profile.interests ?? [])
    .map(normalizeInterest)
    .filter(Boolean)
    .slice(0, MAX_INTERESTS);
  return {
    name:
      profile.name && !PLACEHOLDER_NAMES.has(profile.name)
        ? profile.name.slice(0, MAX_NAME_LENGTH)
        : "",
    nativeLanguage,
    targets,
    levels,
    interests,
    communicationPreference: isCommunicationPreference(profile.communicationPreference)
      ? profile.communicationPreference
      : "natural",
  };
}

/** 某门语言的当前水平；没设置/非法值回落到默认「能聊几句」。 */
export function levelFor(
  levels: Record<string, string> | undefined,
  code: string,
): Level {
  const value = levels?.[code];
  return LEVEL_OPTIONS.includes(value as Level) ? (value as Level) : DEFAULT_LEVEL;
}

/** 设置某门语言的水平（不可变更新，各语言互不影响）。 */
export function withTargetLevel(
  levels: Record<string, string>,
  code: string,
  level: Level,
): Record<string, string> {
  return { ...levels, [code]: level };
}

/**
 * 目标语言多选：母语不可选、最多 3 门。
 *
 * 返回**同一个数组引用**表示这次点击被拒绝（超过上限/选了母语），
 * UI 据此提示「最多 3 门」，不用再重复计算。
 */
export function toggleTarget(
  targets: string[],
  code: string,
  nativeLanguage: string,
): string[] {
  if (!code || code === nativeLanguage) return targets;
  if (targets.includes(code)) return targets.filter((item) => item !== code);
  if (targets.length >= MAX_TARGET_LANGUAGES) return targets;
  return [...targets, code];
}

/** 规范化自定义兴趣：去 #、去首尾空白、合并内部空白、限长。 */
export function normalizeInterest(raw: string): string {
  return raw
    .replace(/^[#＃\s]+/, "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, MAX_INTEREST_LENGTH);
}

/** 点选预设标签：已选则取消；未选且未满 8 个才加入（满了返回原数组引用）。 */
export function toggleInterest(interests: string[], value: string): string[] {
  const tag = normalizeInterest(value);
  if (!tag) return interests;
  if (interests.includes(tag)) return interests.filter((item) => item !== tag);
  if (interests.length >= MAX_INTERESTS) return interests;
  return [...interests, tag];
}

/** 自定义标签：去重（忽略大小写）且最多 8 个；被拒绝时返回原数组引用。 */
export function addCustomInterest(interests: string[], raw: string): string[] {
  const tag = normalizeInterest(raw);
  if (!tag) return interests;
  if (interests.length >= MAX_INTERESTS) return interests;
  if (interests.some((item) => item.toLowerCase() === tag.toLowerCase())) return interests;
  return [...interests, tag];
}

/** 某一步的校验错误；空数组 = 可以继续。 */
export function validateStep(step: OnboardingStep, draft: OnboardingDraft): string[] {
  const errors: string[] = [];
  if (step === 1) {
    if (!draft.name.trim()) errors.push("请填写昵称");
    if (!draft.nativeLanguage.trim()) errors.push("请选择母语");
  }
  if (step === 2) {
    const targets = draft.targets.filter((code) => code !== draft.nativeLanguage);
    if (targets.length === 0) errors.push("至少选择一门想练习的语言");
    if (targets.length > MAX_TARGET_LANGUAGES) {
      errors.push(`最多同时练习 ${MAX_TARGET_LANGUAGES} 门语言`);
    }
  }
  if (step === 3) {
    if (draft.interests.length > MAX_INTERESTS) {
      errors.push(`最多选择 ${MAX_INTERESTS} 个兴趣标签`);
    }
  }
  return errors;
}

/** 第一次出现校验错误的步骤（null = 三步都通过）。 */
export function firstInvalidStep(draft: OnboardingDraft): OnboardingStep | null {
  for (const step of [1, 2, 3] as OnboardingStep[]) {
    if (validateStep(step, draft).length > 0) return step;
  }
  return null;
}

/** 取出 payload 用的目标语言：过滤母语、去重、截断到上限。 */
function payloadTargets(draft: OnboardingDraft): string[] {
  return draft.targets
    .filter((code, index, list) => code !== draft.nativeLanguage && list.indexOf(code) === index)
    .slice(0, MAX_TARGET_LANGUAGES);
}

/** 构造一次 PUT 的完整请求体（字段名对齐后端契约）。 */
export function buildPayload(draft: OnboardingDraft): ProfileSetupPayload {
  const targets = payloadTargets(draft);
  const levels: Record<string, string> = {};
  for (const code of targets) levels[code] = levelFor(draft.levels, code);
  /* 兴趣兜底去重（忽略大小写，保留第一次出现的写法），并截断到上限 */
  const interests: string[] = [];
  const seenInterests = new Set<string>();
  for (const raw of draft.interests) {
    const tag = normalizeInterest(raw);
    if (!tag) continue;
    const key = tag.toLowerCase();
    if (seenInterests.has(key)) continue;
    seenInterests.add(key);
    interests.push(tag);
    if (interests.length >= MAX_INTERESTS) break;
  }
  return {
    nickname: draft.name.trim().slice(0, MAX_NAME_LENGTH),
    native_language: draft.nativeLanguage || "zh",
    target_languages: targets,
    levels,
    interests,
    communication_preference: isCommunicationPreference(draft.communicationPreference)
      ? draft.communicationPreference
      : "natural",
    complete_setup: true,
  };
}

/** 400 profile_incomplete：找到 missing 里第一个已知字段所在的步骤。 */
export function stepForMissing(missing: string[]): OnboardingStep {
  for (const field of missing) {
    const step = MISSING_FIELD_STEP[field];
    if (step) return step;
  }
  return 1;
}

/* ========================================================================== */
/* 组件                                                                       */
/* ========================================================================== */

export function Onboarding() {
  const { profile, updateProfile } = useApp(
    useShallow((state) => ({ profile: state.profile, updateProfile: state.updateProfile })),
  );
  const [draft, setDraft] = useState<OnboardingDraft>(() => createDraft(profile));
  const [step, setStep] = useState<OnboardingStep>(1);
  const [errors, setErrors] = useState<string[]>([]);
  /** 后端 400 时返回的缺失字段名（高亮用） */
  const [missing, setMissing] = useState<string[]>([]);
  const [saveError, setSaveError] = useState("");
  const [saving, setSaving] = useState(false);
  const [interestInput, setInterestInput] = useState("");
  const [interestHint, setInterestHint] = useState("");
  const [targetHint, setTargetHint] = useState("");
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  /* 防重复提交：setSaving 要等下一次渲染才生效，双击/连按回车时用 ref 兜住同 tick 的第二次提交 */
  const inFlight = useRef(false);

  /* 切步骤后回到顶部并聚焦标题：键盘/读屏用户能立刻知道换了一页。 */
  useEffect(() => {
    const scroller = bodyRef.current;
    if (scroller) {
      const reduce =
        typeof window !== "undefined" &&
        typeof window.matchMedia === "function" &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      scroller.scrollTo({ top: 0, behavior: reduce ? "auto" : "smooth" });
    }
    headingRef.current?.focus({ preventScroll: true });
  }, [step]);

  /** 改草稿的统一入口：清掉上一次的校验/保存错误，避免旧红字一直挂着。 */
  const patchDraft = useCallback((patch: Partial<OnboardingDraft>) => {
    setDraft((prev) => ({ ...prev, ...patch }));
    setErrors([]);
    setMissing([]);
    setSaveError("");
    setTargetHint("");
  }, []);

  const submit = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setSaving(true);
    setSaveError("");
    setMissing([]);
    const payload = buildPayload(draft);
    try {
      const result = await saveProfileSetup(payload);
      if (result.ok) {
        haptic("save");
        /* 写回 store：Shell 的闸门看到 setupCompleted === true 会立刻切到主界面 */
        updateProfile({
          name: payload.nickname,
          nativeLanguage: payload.native_language,
          targets: payload.target_languages,
          levels: payload.levels,
          interests: payload.interests,
          communicationPreference:
            payload.communication_preference as Profile["communicationPreference"],
          setupCompleted: true,
          setupCompletedAt: result.setup.completed_at || new Date().toISOString(),
        });
        return;
      }
      haptic("error");
      if (result.error === "incomplete") {
        setMissing(result.missing);
        setStep(stepForMissing(result.missing));
        setSaveError("资料还没填完整，请补上高亮的内容。");
      } else {
        setSaveError("保存失败，请检查网络后重试。");
      }
    } finally {
      inFlight.current = false;
      setSaving(false);
    }
  }, [draft, updateProfile]);

  const handleContinue = () => {
    if (saving) return;
    if (step < 3) {
      const stepErrors = validateStep(step, draft);
      if (stepErrors.length > 0) {
        setErrors(stepErrors);
        haptic("error");
        return;
      }
      haptic("selection");
      setStep((step + 1) as OnboardingStep);
      return;
    }
    /* 最后一步：把三步都再校验一遍；有问题就跳回第一个出错的步骤 */
    const invalidStep = firstInvalidStep(draft);
    if (invalidStep) {
      setErrors(validateStep(invalidStep, draft));
      setStep(invalidStep);
      haptic("error");
      return;
    }
    void submit();
  };

  const handleBack = () => {
    if (saving) return;
    haptic("selection");
    setErrors([]);
    setSaveError("");
    setMissing([]);
    setStep((prev) => Math.max(1, prev - 1) as OnboardingStep);
  };

  const handleNativeLanguage = (code: string) => {
    /* 母语和「在练语言」不能是同一门：切母语时把它从 targets 里移掉 */
    const targets = draft.targets.filter((item) => item !== code);
    const levels = { ...draft.levels };
    for (const target of targets) {
      if (!levels[target]) levels[target] = DEFAULT_LEVEL;
    }
    patchDraft({ nativeLanguage: code, targets, levels });
  };

  const handleToggleTarget = (code: string) => {
    const next = toggleTarget(draft.targets, code, draft.nativeLanguage);
    if (next === draft.targets) {
      setTargetHint(`最多同时练习 ${MAX_TARGET_LANGUAGES} 门语言`);
      return;
    }
    const levels = { ...draft.levels };
    for (const target of next) {
      if (!levels[target]) levels[target] = DEFAULT_LEVEL;
    }
    patchDraft({ targets: next, levels });
  };

  const handleTargetLevel = (code: string, level: Level) => {
    patchDraft({ levels: withTargetLevel(draft.levels, code, level) });
  };

  const handleToggleInterest = (value: string) => {
    const next = toggleInterest(draft.interests, value);
    if (next === draft.interests) {
      setInterestHint(`最多 ${MAX_INTERESTS} 个兴趣标签`);
      return;
    }
    setInterestHint("");
    patchDraft({ interests: next });
  };

  const handleRemoveInterest = (value: string) => {
    setInterestHint("");
    patchDraft({ interests: draft.interests.filter((item) => item !== value) });
  };

  const commitInterest = () => {
    const tag = normalizeInterest(interestInput);
    if (!tag) return;
    if (draft.interests.length >= MAX_INTERESTS) {
      setInterestHint(`最多 ${MAX_INTERESTS} 个兴趣标签`);
      return;
    }
    const next = addCustomInterest(draft.interests, tag);
    if (next === draft.interests) {
      setInterestHint("这个标签已经加过了");
      return;
    }
    setInterestInput("");
    setInterestHint("");
    patchDraft({ interests: next });
  };

  const isMissing = (field: string) => missing.includes(field);
  const presetValues = INTEREST_OPTIONS.map((item) => item.value);
  const customInterests = draft.interests.filter((tag) => !presetValues.includes(tag));
  const optionLanguages = LANGUAGES.filter((item) => item.code !== draft.nativeLanguage);
  const meta = STEP_META[step];

  return (
    <div className="se-onb-screen">
      <div className="se-onb-frame">
        <div className="se-onb-card">
          <header className="se-onb-top">
            <div className="se-onb-brand">
              <Sparkles size={16} strokeWidth={2} aria-hidden />
              <span className="se-onb-wordmark">SONDER</span>
            </div>
            <div
              className="se-onb-progress"
              role="progressbar"
              aria-label="引导进度"
              aria-valuemin={1}
              aria-valuemax={3}
              aria-valuenow={step}
              aria-valuetext={`第 ${step} 步，共 3 步`}
            >
              <span className="se-onb-dots" aria-hidden>
                {([1, 2, 3] as OnboardingStep[]).map((item) => (
                  <span
                    key={item}
                    className={`se-onb-dot ${item === step ? "is-on" : item < step ? "is-done" : ""}`}
                  />
                ))}
              </span>
              <span className="se-onb-count" aria-hidden>
                {step} / 3
              </span>
            </div>
          </header>

          <form
            className="se-onb-form"
            aria-busy={saving}
            onSubmit={(event) => {
              event.preventDefault();
              handleContinue();
            }}
          >
            <div className="se-onb-body" ref={bodyRef}>
              <h1 className="se-onb-title" ref={headingRef} tabIndex={-1}>
                {meta.title}
              </h1>
              <p className="se-onb-subtitle">{meta.subtitle}</p>

              {step === 1 ? (
                <>
                  <div className={`se-onb-field ${isMissing("nickname") ? "is-missing" : ""}`}>
                    <label className="se-onb-label" htmlFor="se-onb-name">
                      怎么称呼你？
                    </label>
                    <input
                      id="se-onb-name"
                      className={`w4-input ${isMissing("nickname") ? "is-error" : ""}`}
                      value={draft.name}
                      maxLength={MAX_NAME_LENGTH}
                      autoComplete="nickname"
                      placeholder="你的昵称"
                      aria-invalid={isMissing("nickname")}
                      onChange={(event) => patchDraft({ name: event.target.value })}
                    />
                    <p className="se-onb-hint">语伴会用这个名字跟你打招呼。</p>
                  </div>
                  <div
                    className={`se-onb-field ${isMissing("native_language") ? "is-missing" : ""}`}
                  >
                    <label className="se-onb-label" htmlFor="se-onb-native">
                      你的母语是？
                    </label>
                    <select
                      id="se-onb-native"
                      className={`w4-select ${isMissing("native_language") ? "is-error" : ""}`}
                      value={draft.nativeLanguage}
                      aria-invalid={isMissing("native_language")}
                      onChange={(event) => handleNativeLanguage(event.target.value)}
                    >
                      {NATIVE_LANGUAGE_OPTIONS.map((item) => (
                        <option key={item.code} value={item.code}>
                          {item.label}
                        </option>
                      ))}
                    </select>
                    <p className="se-onb-hint">解释和翻译会用这门语言；母语不会出现在目标语言里。</p>
                  </div>
                </>
              ) : null}

              {step === 2 ? (
                <>
                  <div
                    className={`se-onb-field ${isMissing("target_languages") ? "is-missing" : ""}`}
                  >
                    <span className="se-onb-label" id="se-onb-targets-label">
                      想练哪些语言？最多 {MAX_TARGET_LANGUAGES} 门
                    </span>
                    <div
                      className="se-onb-chips"
                      role="group"
                      aria-labelledby="se-onb-targets-label"
                    >
                      {optionLanguages.map((language) => {
                        const active = draft.targets.includes(language.code);
                        return (
                          <button
                            key={language.code}
                            type="button"
                            className={`w4-chip ${active ? "is-green" : ""}`}
                            aria-pressed={active}
                            onClick={() => handleToggleTarget(language.code)}
                          >
                            {language.flag} {language.label}
                          </button>
                        );
                      })}
                    </div>
                    {targetHint ? (
                      <p className="se-onb-hint is-warn" role="status">
                        {targetHint}
                      </p>
                    ) : (
                      <p className="se-onb-hint">
                        已选 {draft.targets.length} / {MAX_TARGET_LANGUAGES} 门
                      </p>
                    )}
                  </div>

                  {draft.targets.length === 0 ? (
                    <p className="se-onb-empty">先选一门语言，再设置你的水平。</p>
                  ) : null}

                  {draft.targets.map((code) => {
                    const language = LANGUAGES.find((item) => item.code === code);
                    const current = levelFor(draft.levels, code);
                    const label = language ? `${language.flag} ${language.label}` : code;
                    return (
                      <div className="se-onb-level" key={`level-${code}`}>
                        <div className="se-onb-level-head">
                          <span className="se-onb-level-name">{label}</span>
                          <span className="se-onb-level-value">{current}</span>
                        </div>
                        <div className="se-onb-levels" role="group" aria-label={`${label} 的水平`}>
                          {LEVEL_OPTIONS.map((level) => (
                            <button
                              key={level}
                              type="button"
                              className="se-onb-level-btn"
                              aria-pressed={current === level}
                              onClick={() => handleTargetLevel(code, level)}
                            >
                              {level}
                            </button>
                          ))}
                        </div>
                      </div>
                    );
                  })}
                </>
              ) : null}

              {step === 3 ? (
                <>
                  <div
                    className={`se-onb-field ${isMissing("interests") ? "is-missing" : ""}`}
                  >
                    <span className="se-onb-label" id="se-onb-interests-label">
                      选几个喜欢的话题（最多 {MAX_INTERESTS} 个）
                    </span>
                    <div
                      className="se-onb-chips"
                      role="group"
                      aria-labelledby="se-onb-interests-label"
                    >
                      {INTEREST_OPTIONS.map((item) => {
                        const active = draft.interests.includes(item.value);
                        return (
                          <button
                            key={item.value}
                            type="button"
                            className={`w4-chip ${active ? "is-green" : ""}`}
                            aria-pressed={active}
                            onClick={() => handleToggleInterest(item.value)}
                          >
                            {item.label}
                          </button>
                        );
                      })}
                    </div>
                    {customInterests.length > 0 ? (
                      <div className="se-onb-chips se-onb-chips-custom" aria-label="自定义话题">
                        {customInterests.map((tag) => (
                          <button
                            key={`custom-${tag}`}
                            type="button"
                            className="w4-chip is-green"
                            aria-pressed
                            title="点击删除"
                            onClick={() => handleRemoveInterest(tag)}
                          >
                            {tag} ×
                          </button>
                        ))}
                      </div>
                    ) : null}
                    <div className="se-onb-custom">
                      <input
                        className="w4-input"
                        value={interestInput}
                        maxLength={MAX_INTEREST_LENGTH}
                        placeholder="或者自己写一个，按回车添加"
                        aria-label="自定义兴趣标签"
                        onChange={(event) => setInterestInput(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key !== "Enter") return;
                          event.preventDefault();
                          commitInterest();
                        }}
                      />
                      <button
                        type="button"
                        className="w4-btn"
                        disabled={!interestInput.trim() || draft.interests.length >= MAX_INTERESTS}
                        onClick={commitInterest}
                      >
                        添加
                      </button>
                    </div>
                    <p className="se-onb-hint" role="status">
                      {draft.interests.length} / {MAX_INTERESTS}
                      {interestHint ? ` · ${interestHint}` : ""}
                    </p>
                  </div>

                  <div
                    className={`se-onb-field ${isMissing("communication_preference") ? "is-missing" : ""}`}
                  >
                    <span className="se-onb-label">
                      How would you like your language partner to help you?
                    </span>
                    <div className="se-onb-choice" role="group" aria-label="语伴纠错偏好">
                      {COMMUNICATION_OPTIONS.map((option) => {
                        const active = draft.communicationPreference === option.value;
                        return (
                          <button
                            key={option.value}
                            type="button"
                            className="se-onb-choice-btn"
                            aria-pressed={active}
                            onClick={() => patchDraft({ communicationPreference: option.value })}
                          >
                            <span className="se-onb-choice-title">
                              {active ? <Check size={14} strokeWidth={2.6} aria-hidden /> : null}
                              {option.title}
                            </span>
                            <span className="se-onb-choice-hint">{option.hint}</span>
                          </button>
                        );
                      })}
                    </div>
                  </div>
                </>
              ) : null}
            </div>

            {errors.length > 0 || saveError ? (
              <div className="se-onb-errors" role="alert">
                {errors.length > 0 ? (
                  <ul>
                    {errors.map((message) => (
                      <li key={message}>{message}</li>
                    ))}
                  </ul>
                ) : null}
                {saveError ? (
                  <>
                    <p className="se-onb-errors-text">{saveError}</p>
                    <button
                      type="button"
                      className="se-onb-retry"
                      disabled={saving}
                      onClick={() => void submit()}
                    >
                      重试
                    </button>
                  </>
                ) : null}
              </div>
            ) : null}

            <footer className="se-onb-foot">
              <button
                type="button"
                className="w4-btn se-onb-back"
                onClick={handleBack}
                disabled={step === 1 || saving}
              >
                <ArrowLeft size={16} aria-hidden />
                返回
              </button>
              <button
                type="submit"
                className="w4-btn is-primary se-onb-next"
                disabled={saving}
              >
                {saving ? (
                  <>
                    <Loader2 className="se-onb-spin" size={16} aria-hidden />
                    {step === 3 ? "正在保存…" : "保存中…"}
                  </>
                ) : (
                  <>
                    {step === 3 ? "开始认识朋友" : "继续"}
                    <ArrowRight size={16} aria-hidden />
                  </>
                )}
              </button>
            </footer>
          </form>
        </div>
      </div>
    </div>
  );
}
