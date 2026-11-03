"use client";

/** 表达本 · 画像 Tab：待掌握/已掌握表达 + 我的记忆卡（可编辑/删除条目）。 */

import { useEffect, useMemo, useState } from "react";
import {
  BookOpen,
  BrainCircuit,
  Check,
  CheckCheck,
  Loader2,
  Save,
  Star,
  Trash2,
  X,
} from "lucide-react";

import { Avatar } from "@/components/v2/Avatar";
import { useToast } from "@/components/v2/Toast";
import { EmptyState, ErrorBanner, Spinner } from "@/components/v2/ui";
import { closenessLabel, errorText, langLabel } from "@/lib/v2/format";
import type { Expression, FriendMemory, MemoryPayload } from "@/lib/v2/types";

interface NotesTabProps {
  expressions: Expression[];
  expressionsLoading: boolean;
  expressionsError: string | null;
  onReloadExpressions: () => void;
  onToggleExpression: (expression: Expression) => Promise<void>;
  onDeleteExpression: (expression: Expression) => Promise<void>;
  memories: MemoryPayload | null;
  memoriesLoading: boolean;
  memoriesError: string | null;
  onReloadMemories: () => void;
  onSaveMemories: (patch: {
    nickname: string;
    city: string;
    majorOrJob: string;
    interests: string[];
    weaknesses: string[];
    /** 正在学的语言 */
    targetLanguages: string[];
    /** 母语 */
    nativeLanguage: string;
  }) => Promise<void>;
}

type Segment = "expressions" | "memories";

/** 可选语言（与后端声线原型的 native_lang 对齐）。 */
const LANGUAGE_OPTIONS: Array<{ code: string; label: string; flag: string }> = [
  { code: "en", label: "英语", flag: "🇬🇧" },
  { code: "ja", label: "日语", flag: "🇯🇵" },
  { code: "ko", label: "韩语", flag: "🇰🇷" },
  { code: "fr", label: "法语", flag: "🇫🇷" },
  { code: "de", label: "德语", flag: "🇩🇪" },
  { code: "it", label: "意大利语", flag: "🇮🇹" },
  { code: "es", label: "西班牙语", flag: "🇪🇸" },
  { code: "pt", label: "葡萄牙语", flag: "🇵🇹" },
  { code: "zh-CN", label: "中文", flag: "🇨🇳" },
];

const langText = (code: string) => LANGUAGE_OPTIONS.find((o) => o.code === code)?.label ?? code;

/** 语言 chip 选择器。 */
function LanguageChips({
  value,
  options,
  onToggle,
}: {
  value: string[];
  options: Array<{ code: string; label: string; flag: string }>;
  onToggle: (code: string) => void;
}) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {options.map((opt) => {
        const on = value.includes(opt.code);
        return (
          <button
            key={opt.code}
            type="button"
            onClick={() => onToggle(opt.code)}
            aria-pressed={on}
            className={`rounded-full border px-2.5 py-1 text-[12px] transition-colors ${
              on
                ? "border-[color:var(--accent)] bg-[color:var(--accent)]/10 text-[color:var(--accent)]"
                : "border-[color:var(--line)] text-[color:var(--muted)]"
            }`}
          >
            <span className="mr-1">{opt.flag}</span>
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}

function ExpressionCard({
  expression,
  busy,
  onToggle,
  onDelete,
}: {
  expression: Expression;
  busy: boolean;
  onToggle: () => void;
  onDelete: () => void;
}) {
  const mastered = expression.status === "mastered";
  return (
    <article className={`se-expression ${mastered ? "mastered" : ""}`}>
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <p className="se-expression-original">{expression.original_text || "（原句缺失）"}</p>
          <p className="se-expression-better">
            <span className="se-expression-arrow" aria-hidden>
              →
            </span>
            {expression.better_expression}
          </p>
          {expression.explanation ? <p className="se-expression-explain">{expression.explanation}</p> : null}
        </div>
        <div className="flex shrink-0 flex-col items-center gap-1.5">
          <button
            type="button"
            onClick={onToggle}
            disabled={busy}
            aria-label={mastered ? "标记为待掌握" : "标记为已掌握"}
            title={mastered ? "已掌握（点击撤销）" : "标记掌握"}
            className={`se-exp-btn ${mastered ? "done" : ""}`}
          >
            {busy ? <Loader2 size={13} className="animate-spin" /> : mastered ? <CheckCheck size={13} /> : <Check size={13} />}
          </button>
          <button type="button" onClick={onDelete} disabled={busy} aria-label="删除表达" className="se-exp-btn danger">
            <Trash2 size={13} />
          </button>
        </div>
      </div>
      <div className="mt-2 flex items-center gap-2 text-[10.5px] text-[color:var(--se-faint)]">
        {expression.friend_name ? <span>来自 {expression.friend_name}</span> : null}
        {expression.mastery_count > 0 ? <span>复习 {expression.mastery_count} 次</span> : null}
        <span className={`ml-auto rounded-full px-1.5 py-0.5 ${mastered ? "bg-[#e9f9ef] text-[#0d7a3c]" : "bg-[#fff7ea] text-[#b57418]"}`}>
          {mastered ? "已掌握" : "待掌握"}
        </span>
      </div>
    </article>
  );
}

export function NotesTab({
  expressions,
  expressionsLoading,
  expressionsError,
  onReloadExpressions,
  onToggleExpression,
  onDeleteExpression,
  memories,
  memoriesLoading,
  memoriesError,
  onReloadMemories,
  onSaveMemories,
}: NotesTabProps) {
  const { showToast } = useToast();
  const [segment, setSegment] = useState<Segment>("expressions");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);

  const [nickname, setNickname] = useState("");
  const [city, setCity] = useState("");
  const [majorOrJob, setMajorOrJob] = useState("");
  const [interests, setInterests] = useState<string[]>([]);
  const [weaknesses, setWeaknesses] = useState<string[]>([]);
  const [targetLanguages, setTargetLanguages] = useState<string[]>(["en"]);
  const [nativeLanguage, setNativeLanguage] = useState<string>("zh-CN");

  const fact = memories?.fact_sheet ?? null;

  useEffect(() => {
    if (!memories) return;
    setNickname(memories.profile.nickname || memories.fact_sheet.profile.nickname || "");
    setCity(memories.fact_sheet.profile.city);
    setMajorOrJob(memories.fact_sheet.profile.major_or_job);
    setInterests(memories.fact_sheet.interests);
    setWeaknesses(memories.fact_sheet.language_profile.weaknesses);
    setTargetLanguages(
      Array.isArray(memories.profile.target_languages) && memories.profile.target_languages.length
        ? memories.profile.target_languages
        : ["en"],
    );
    setNativeLanguage(memories.profile.native_language || "zh-CN");
    setDirty(false);
  }, [memories]);

  const learning = useMemo(() => expressions.filter((item) => item.status !== "mastered"), [expressions]);
  const mastered = useMemo(() => expressions.filter((item) => item.status === "mastered"), [expressions]);

  const toggle = async (expression: Expression) => {
    setBusyId(expression.id);
    try {
      await onToggleExpression(expression);
    } catch {
      /* 错误已由上层 toast 提示 */
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (expression: Expression) => {
    setBusyId(expression.id);
    try {
      await onDeleteExpression(expression);
    } catch {
      /* 错误已由上层 toast 提示 */
    } finally {
      setBusyId(null);
    }
  };

  const save = async () => {
    setSaving(true);
    try {
      await onSaveMemories({
        nickname,
        city,
        majorOrJob,
        interests,
        weaknesses,
        targetLanguages,
        nativeLanguage,
      });
      setDirty(false);
    } catch (err) {
      showToast(`保存失败：${errorText(err)}`, "error");
    } finally {
      setSaving(false);
    }
  };

  const touch = () => setDirty(true);

  return (
    <div className="pb-4">
      {/* 分段控件 */}
      <div className="sticky top-0 z-10 bg-[color:var(--se-bg)] px-3 pb-2 pt-2">
        <div className="se-segment">
          <button
            type="button"
            onClick={() => setSegment("expressions")}
            className={segment === "expressions" ? "active" : ""}
          >
            <BookOpen size={14} />
            表达本
            {expressions.length > 0 ? <span className="se-segment-count">{learning.length}</span> : null}
          </button>
          <button
            type="button"
            onClick={() => setSegment("memories")}
            className={segment === "memories" ? "active" : ""}
          >
            <BrainCircuit size={14} />
            我的记忆
          </button>
        </div>
      </div>

      {segment === "expressions" ? (
        <div className="px-3">
          {expressionsError ? (
            <ErrorBanner message={`表达本加载失败：${expressionsError}`} onRetry={onReloadExpressions} />
          ) : null}
          {expressionsLoading && expressions.length === 0 ? <Spinner label="加载表达本…" /> : null}
          {!expressionsLoading && expressions.length === 0 && !expressionsError ? (
            <EmptyState
              title="表达本还是空的"
              hint="聊天时点开「✨ 地道表达」并收藏，这里就会攒下你的语料。"
              icon={<BookOpen size={26} strokeWidth={1.6} />}
            />
          ) : null}

          {learning.length > 0 ? (
            <>
              <div className="flex items-center gap-1.5 px-1 pb-1.5 pt-3 text-[12px] font-semibold text-[color:var(--se-dim)]">
                <Star size={12} className="text-[#e8a33d]" />
                待掌握 · {learning.length}
              </div>
              <div className="flex flex-col gap-2">
                {learning.map((expression) => (
                  <ExpressionCard
                    key={expression.id}
                    expression={expression}
                    busy={busyId === expression.id}
                    onToggle={() => void toggle(expression)}
                    onDelete={() => void remove(expression)}
                  />
                ))}
              </div>
            </>
          ) : null}

          {mastered.length > 0 ? (
            <>
              <div className="flex items-center gap-1.5 px-1 pb-1.5 pt-4 text-[12px] font-semibold text-[color:var(--se-dim)]">
                <CheckCheck size={12} className="text-[var(--se-green)]" />
                已掌握 · {mastered.length}
              </div>
              <div className="flex flex-col gap-2">
                {mastered.map((expression) => (
                  <ExpressionCard
                    key={expression.id}
                    expression={expression}
                    busy={busyId === expression.id}
                    onToggle={() => void toggle(expression)}
                    onDelete={() => void remove(expression)}
                  />
                ))}
              </div>
            </>
          ) : null}

          <p className="mt-4 text-center text-[10.5px] leading-relaxed text-[color:var(--se-faint)]">
            表达会在聊天中被记忆模块挑选为「待复习」注入上下文
          </p>
        </div>
      ) : (
        <div className="px-3">
          {memoriesError ? <ErrorBanner message={`记忆加载失败：${memoriesError}`} onRetry={onReloadMemories} /> : null}
          {memoriesLoading && !memories ? <Spinner label="加载记忆卡…" /> : null}

          {memories ? (
            <div className="flex flex-col gap-2.5 pt-1">
              {/* 昵称/城市/专业 */}
              <div className="se-card px-3.5 py-3">
        {/* 语言设置：决定「偶遇」优先给什么语种的语伴 */}
        <p className="mb-1 text-[13px] font-medium">我在学</p>
        <p className="mb-2 text-[11px] text-[color:var(--muted)]">
          偶遇会优先抽这些语言的语伴（约 7 成），偶尔给点惊喜。
        </p>
        <LanguageChips
          value={targetLanguages}
          options={LANGUAGE_OPTIONS.filter((o) => o.code !== "zh-CN")}
          onToggle={(code) =>
            setTargetLanguages((prev) =>
              prev.includes(code)
                ? prev.length > 1
                  ? prev.filter((c) => c !== code)
                  : prev
                : [...prev, code],
            )
          }
        />
        <p className="mb-1 mt-3 text-[13px] font-medium">我会说</p>
        <LanguageChips
          value={[nativeLanguage]}
          options={LANGUAGE_OPTIONS.filter((o) => o.code === "zh-CN" || o.code === "en")}
          onToggle={(code) => setNativeLanguage(code)}
        />
        <p className="mb-3 mt-2 text-[11px] text-[color:var(--muted)]">
          正在学 <b>{targetLanguages.map(langText).join("、") || "未设置"}</b> · 母语{" "}
          <b>{langText(nativeLanguage)}</b>
        </p>
                <div className="flex items-center gap-3">
                  <Avatar name={nickname || "我"} seed="me-speakeasy" size={46} flag={false} />
                  <div className="min-w-0 flex-1">
                    <p className="text-[14px] font-semibold">我的人设</p>
                    <p className="mt-0.5 text-[11px] text-[color:var(--se-dim)]">
                      母语 {langLabel(memories.profile.native_language)} · 目标{" "}
                      {memories.profile.target_languages.map(langLabel).join(" / ") || "英语"}
                    </p>
                  </div>
                </div>

                <div className="mt-3 grid grid-cols-2 gap-2">
                  <label className="se-field col-span-2">
                    <span>昵称</span>
                    <input
                      value={nickname}
                      onChange={(event) => {
                        setNickname(event.target.value);
                        touch();
                      }}
                      placeholder="好友怎么称呼你"
                    />
                  </label>
                  <label className="se-field">
                    <span>城市</span>
                    <input
                      value={city}
                      onChange={(event) => {
                        setCity(event.target.value);
                        touch();
                      }}
                      placeholder="如：Shanghai"
                    />
                  </label>
                  <label className="se-field">
                    <span>专业 / 职业</span>
                    <input
                      value={majorOrJob}
                      onChange={(event) => {
                        setMajorOrJob(event.target.value);
                        touch();
                      }}
                      placeholder="如：CS student"
                    />
                  </label>
                </div>
              </div>

              {/* 兴趣 + 语言弱点 */}
              <div className="se-card px-3.5 py-3">
                <p className="se-sheet-label">兴趣</p>
                {interests.length === 0 ? (
                  <p className="mt-1 text-[11.5px] text-[color:var(--se-faint)]">还没有记录，聊天后会自动提炼。</p>
                ) : (
                  <div className="mt-1.5 flex flex-wrap gap-1.5">
                    {interests.map((item) => (
                      <span key={item} className="se-memory-chip interest">
                        {item}
                        <button
                          type="button"
                          aria-label={`删除兴趣 ${item}`}
                          onClick={() => {
                            setInterests((prev) => prev.filter((value) => value !== item));
                            touch();
                          }}
                        >
                          <X size={11} />
                        </button>
                      </span>
                    ))}
                  </div>
                )}

                <p className="se-sheet-label mt-3.5">语言弱点</p>
                {weaknesses.length === 0 ? (
                  <p className="mt-1 text-[11.5px] text-[color:var(--se-faint)]">还没有记录。</p>
                ) : (
                  <div className="mt-1.5 flex flex-wrap gap-1.5">
                    {weaknesses.map((item) => (
                      <span key={item} className="se-memory-chip weakness">
                        {item}
                        <button
                          type="button"
                          aria-label={`删除弱点 ${item}`}
                          onClick={() => {
                            setWeaknesses((prev) => prev.filter((value) => value !== item));
                            touch();
                          }}
                        >
                          <X size={11} />
                        </button>
                      </span>
                    ))}
                  </div>
                )}

                {(fact?.recent_events?.length ?? 0) > 0 ? (
                  <>
                    <p className="se-sheet-label mt-3.5">最近发生的事</p>
                    <ul className="mt-1 space-y-1">
                      {fact?.recent_events.map((event) => (
                        <li key={event} className="text-[11.5px] leading-relaxed text-[color:var(--se-dim)]">
                          · {event}
                        </li>
                      ))}
                    </ul>
                  </>
                ) : null}
              </div>

              {/* 好友记忆 */}
              {memories.friends.length > 0 ? (
                <div className="se-card px-3.5 py-3">
                  <p className="se-sheet-label">好友记忆</p>
                  <div className="mt-2 flex flex-col gap-3">
                    {memories.friends.map((friend: FriendMemory) => (
                      <div key={friend.id} className="border-l-2 border-black/[0.06] pl-2.5">
                        <div className="flex items-center gap-1.5">
                          <Avatar name={friend.name} seed={friend.avatar_seed || friend.name} size={24} />
                          <span className="text-[12.5px] font-medium">{friend.name}</span>
                          <span className="text-[10.5px] text-[color:var(--se-faint)]">
                            {closenessLabel(friend.closeness_level)}
                          </span>
                        </div>
                        {friend.shared_episodes.length > 0 ? (
                          <ul className="mt-1 space-y-0.5">
                            {friend.shared_episodes.slice(0, 3).map((episode) => (
                              <li key={episode} className="text-[11px] leading-relaxed text-[color:var(--se-dim)]">
                                · {episode}
                              </li>
                            ))}
                          </ul>
                        ) : null}
                        {friend.open_loops.length > 0 ? (
                          <div className="mt-1 flex flex-wrap gap-1">
                            {friend.open_loops.slice(0, 3).map((loop) => (
                              <span key={loop.event} className="se-open-loop">
                                {loop.event}
                              </span>
                            ))}
                          </div>
                        ) : null}
                      </div>
                    ))}
                  </div>
                </div>
              ) : null}

              <button
                type="button"
                onClick={() => void save()}
                disabled={saving}
                className="se-save-btn"
              >
                {saving ? <Loader2 size={15} className="animate-spin" /> : <Save size={15} />}
                {dirty ? "保存修改" : "保存"}
                {dirty ? <span className="se-dirty-dot" aria-label="有未保存修改" /> : null}
              </button>
              <p className="text-center text-[10.5px] leading-relaxed text-[color:var(--se-faint)]">
                记忆卡会注入每轮对话的上下文 · 手动纠错立即生效
              </p>
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}
