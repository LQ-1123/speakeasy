"use client";

import { useShallow } from "zustand/react/shallow";

/**
 * 表达本：从聊天里收藏的地道说法。
 * 顶部只放「今天该练」这一张卡：当前句子最大，原句 / 原因退到次要位置，
 * 下面才是收藏列表（来源、标签、掌握度）。点「开始复习」进入逐句复习。
 */

import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import {
  BookMarked,
  Check,
  CircleCheck,
  RotateCcw,
  Sparkles,
  Star,
  Trash2,
  X,
} from "lucide-react";

import { haptic } from "../../theme/haptics";
import type { Expression } from "../model";
import { useApp } from "../store";
import { Avatar, Empty } from "../ui";
import "../styles/profile.css";

type ExpressionFilter = "all" | "learning" | "mastered";

function noteDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  if (date.toDateString() === now.toDateString()) return "今天";
  return date.toLocaleDateString("zh-CN", { month: "long", day: "numeric" });
}

const FILTERS: Array<{ value: ExpressionFilter; label: string }> = [
  { value: "all", label: "全部" },
  { value: "learning", label: "在练" },
  { value: "mastered", label: "已掌握" },
];

export function NotesPane() {
  const {
    expressions,
    friendById,
    practiceExpression,
    toggleMastered,
    removeExpression,
  } = useApp(
    useShallow((state) => ({
      expressions: state.expressions,
      friendById: state.friendById,
      practiceExpression: state.practiceExpression,
      toggleMastered: state.toggleMastered,
      removeExpression: state.removeExpression,
    })),
  );
  const [filter, setFilter] = useState<ExpressionFilter>("all");
  const [reviewOpen, setReviewOpen] = useState(false);

  const learning = useMemo(
    () => expressions.filter((e) => e.status === "learning"),
    [expressions],
  );
  const mastered = useMemo(
    () => expressions.filter((e) => e.status === "mastered"),
    [expressions],
  );
  const dueQueue = useMemo(() => [...learning].sort((a, b) => b.due - a.due), [learning]);

  const list = filter === "learning" ? learning : filter === "mastered" ? mastered : expressions;
  const progress = expressions.length
    ? Math.round((mastered.length / expressions.length) * 100)
    : 0;
  const current = dueQueue[0];
  const counts: Record<ExpressionFilter, number> = {
    all: expressions.length,
    learning: learning.length,
    mastered: mastered.length,
  };

  return (
    <div className="w4-main">
      <div className="w4-head">
        <div className="w4-head-main">
          <div className="w4-head-title">表达本</div>
          <div className="w4-head-sub">收藏的句子，练到能脱口而出</div>
        </div>
      </div>

      <div className="w4-scroll">
        <div className="w4-notes">
          <section className="w4-notes-hero" aria-label="今天该练">
            <div className="w4-notes-head-row">
              <span className="w4-notes-eyebrow">
                <Sparkles size={13} strokeWidth={1.9} />
                今天该练
              </span>
              <span className="w4-notes-count">
                已掌握 {mastered.length} / {expressions.length}
              </span>
            </div>

            {current ? (
              <>
                <p className="w4-notes-current">{current.better}</p>
                <p className="w4-notes-original">你上次说 “{current.original}”</p>
                {current.why ? <p className="w4-notes-why">{current.why}</p> : null}
                <div className="w4-notes-actions">
                  <button
                    className="w4-btn is-primary"
                    onClick={() => {
                      haptic("selection");
                      setReviewOpen(true);
                    }}
                  >
                    <RotateCcw size={15} strokeWidth={1.9} />
                    开始复习
                  </button>
                  <span className="w4-notes-left">还有 {dueQueue.length} 句等着你</span>
                </div>
              </>
            ) : (
              <div className="w4-notes-rest">
                <span className="w4-notes-rest-icon">
                  <CircleCheck size={20} strokeWidth={1.7} />
                </span>
                <div>
                  <p className="w4-notes-current">今天都练完了</p>
                  <p className="w4-notes-original">
                    {expressions.length
                      ? `你已经掌握 ${mastered.length} 句，明天它们会再出现一次。`
                      : "聊天时收进表达本的句子，会在这里排队等你复习。"}
                  </p>
                </div>
              </div>
            )}

            <div
              className="w4-notes-progress"
              role="progressbar"
              aria-valuenow={progress}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-label="掌握进度"
            >
              <i style={{ width: `${progress}%` }} />
            </div>
          </section>

          <div className="w4-notes-list-head">
            <h2 className="w4-notes-list-title">收藏的句子</h2>
            <div className="w4-notes-tabs" role="tablist" aria-label="筛选表达">
              {FILTERS.map((item) => (
                <button
                  key={item.value}
                  role="tab"
                  aria-selected={filter === item.value}
                  className={`w4-notes-tab ${filter === item.value ? "on" : ""}`}
                  onClick={() => {
                    haptic("selection");
                    setFilter(item.value);
                  }}
                >
                  {item.label}
                  <span className="w4-notes-tab-count">{counts[item.value]}</span>
                </button>
              ))}
            </div>
          </div>

          {list.length === 0 ? (
            <div className="w4-notes-empty">
              <Empty
                icon={<BookMarked size={24} strokeWidth={1.5} />}
                title="这里还是空的"
                text="聊天时好友给你的建议，点「收进表达本」就会出现在这里。"
              />
            </div>
          ) : (
            <div className="w4-notes-list">
              {list.map((expression) => {
                const source = friendById(expression.sourceFriendId);
                const isMastered = expression.status === "mastered";
                return (
                  <article
                    key={expression.id}
                    className={`w4-note ${isMastered ? "is-mastered" : ""}`}
                  >
                    <div className="w4-note-top">
                      <span className="w4-note-source">
                        {source ? (
                          <Avatar friend={source} size={18} round />
                        ) : (
                          <BookMarked size={13} strokeWidth={1.9} />
                        )}
                        <b>{source ? `${source.name} 教你的` : "手动收藏"}</b>
                      </span>
                      <span aria-hidden>·</span>
                      <span className="w4-note-date">{noteDate(expression.createdAt)}</span>
                      <span
                        className="w4-note-mastery"
                        title={`掌握度 ${expression.mastery}/3`}
                        aria-label={`掌握度 ${expression.mastery}/3`}
                      >
                        {[0, 1, 2].map((index) => (
                          <i key={index} className={index < expression.mastery ? "on" : ""} />
                        ))}
                      </span>
                    </div>

                    <p className="w4-note-better">{expression.better}</p>
                    <p className="w4-note-original">原来：{expression.original}</p>
                    {expression.why ? <p className="w4-note-why">{expression.why}</p> : null}

                    <div className="w4-note-foot">
                      <span className={`w4-note-status ${isMastered ? "is-mastered" : ""}`}>
                        {isMastered ? (
                          <>
                            <Check size={11} strokeWidth={2.6} />
                            已掌握
                          </>
                        ) : (
                          "在练"
                        )}
                      </span>
                      {isMastered ? (
                        <button
                          className="w4-note-practice"
                          onClick={() => {
                            haptic("selection");
                            toggleMastered(expression.id);
                          }}
                        >
                          重新练
                        </button>
                      ) : (
                        <button
                          className="w4-note-practice"
                          onClick={() => {
                            haptic("selection");
                            setReviewOpen(true);
                          }}
                        >
                          练一下
                        </button>
                      )}
                      <button
                        className="w4-note-remove"
                        aria-label="从表达本删除"
                        onClick={() => {
                          haptic("selection");
                          removeExpression(expression.id);
                        }}
                      >
                        <Trash2 size={14} strokeWidth={1.9} />
                      </button>
                    </div>
                  </article>
                );
              })}
            </div>
          )}
        </div>
      </div>

      {reviewOpen && dueQueue.length ? (
        <ReviewOverlay
          queue={dueQueue}
          onClose={() => setReviewOpen(false)}
          onAnswer={(id, remembered) => practiceExpression(id, remembered)}
        />
      ) : null}
    </div>
  );
}

function ReviewOverlay({
  queue,
  onClose,
  onAnswer,
}: {
  queue: Expression[];
  onClose: () => void;
  onAnswer: (id: string, remembered: boolean) => void;
}) {
  const [index, setIndex] = useState(0);
  const [revealed, setRevealed] = useState(false);
  const [done, setDone] = useState(0);
  const item = queue[index];

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const node = !item ? (
    <div className="w4-review" role="dialog" aria-modal="true" aria-label="复习完成">
      <div className="w4-review-head">
        <button className="w4-icon-btn" onClick={onClose} aria-label="关闭">
          <X size={17} />
        </button>
        <span className="w4-review-title">今天练完了</span>
        <div className="w4-review-spacer" />
      </div>
      <div className="w4-review-body">
        <div className="w4-review-card is-done">
          <div className="w4-review-done-icon">
            <CircleCheck size={30} strokeWidth={1.6} />
          </div>
          <p className="w4-review-done-title">练了 {done} 句</p>
          <p className="w4-review-hint">明天这个时候，它们会再出现一次。</p>
          <div className="w4-review-actions">
            <button className="w4-btn is-primary" onClick={onClose}>
              回到表达本
            </button>
          </div>
        </div>
      </div>
    </div>
  ) : (
    <div className="w4-review" role="dialog" aria-modal="true" aria-label="复习表达">
      <div className="w4-review-head">
        <button className="w4-icon-btn" onClick={onClose} aria-label="关闭复习">
          <X size={17} />
        </button>
        <span className="w4-review-title">
          复习 · {index + 1}/{queue.length}
        </span>
        <div className="w4-review-spacer" />
        <span className="w4-review-count">已练 {done}</span>
      </div>
      <div
        className="w4-review-progress"
        role="progressbar"
        aria-valuenow={index}
        aria-valuemin={0}
        aria-valuemax={queue.length}
        aria-label="复习进度"
      >
        <i style={{ width: `${(index / queue.length) * 100}%` }} />
      </div>
      <div className="w4-review-body">
        <div className="w4-review-card" key={item.id}>
          <div className="w4-review-label">你上次说</div>
          <p className="w4-review-q">“{item.original}”</p>
          <p className="w4-review-hint">更地道的说法是？</p>

          {revealed ? (
            <div className="w4-review-a">
              <p className="w4-review-a-main">{item.better}</p>
              {item.why ? <p className="w4-review-a-why">{item.why}</p> : null}
              <div className="w4-review-actions">
                <button
                  className="w4-btn"
                  onClick={() => {
                    haptic("selection");
                    onAnswer(item.id, false);
                    setRevealed(false);
                    setIndex(index + 1);
                    setDone((value) => value + 1);
                  }}
                >
                  还不太熟
                </button>
                <button
                  className="w4-btn is-primary"
                  onClick={() => {
                    haptic("save");
                    onAnswer(item.id, true);
                    setRevealed(false);
                    setIndex(index + 1);
                    setDone((value) => value + 1);
                  }}
                >
                  记住了
                </button>
              </div>
            </div>
          ) : (
            <div className="w4-review-actions">
              <button
                className="w4-btn is-primary"
                onClick={() => {
                  haptic("selection");
                  setRevealed(true);
                }}
              >
                <Star size={15} strokeWidth={1.9} />
                想一想，看答案
              </button>
            </div>
          )}
        </div>
      </div>
      <div className="w4-review-foot">在聊天里自然地用出来，比在这里背更有用。</div>
    </div>
  );

  return typeof document === "undefined" ? node : createPortal(node, document.body);
}
