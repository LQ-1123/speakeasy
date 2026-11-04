"use client";

/**
 * 表达本：从聊天里收藏的地道说法。
 * 重点不是「列表」，而是「今天该练哪几条」——所以顶部是进度，
 * 列表按复习紧迫度排，点「练一下」进入一句一句的复习。
 */

import { useMemo, useState } from "react";
import { BookMarked, Check, CircleCheck, RotateCcw, Star, Trash2, X } from "lucide-react";

import type { Expression } from "../model";
import { useApp } from "../store";
import { Avatar, Empty, Icon } from "../ui";

export function NotesPane() {
  const { expressions, friends, friendById, practiceExpression, toggleMastered, removeExpression, profile } =
    useApp();
  const [filter, setFilter] = useState<"all" | "learning" | "mastered">("all");
  const [reviewOpen, setReviewOpen] = useState(false);

  const learning = expressions.filter((e) => e.status === "learning");
  const mastered = expressions.filter((e) => e.status === "mastered");
  const dueQueue = useMemo(
    () => [...learning].sort((a, b) => b.due - a.due),
    [learning],
  );

  const list = filter === "learning" ? learning : filter === "mastered" ? mastered : expressions;
  const progress = expressions.length ? Math.round((mastered.length / expressions.length) * 100) : 0;

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
          <div className="w4-notes-hero">
            <div className="w4-notes-hero-top">
              <div>
                <div className="w4-notes-big">
                  {dueQueue.length}
                  <small>条今天该练</small>
                </div>
                <div className="w4-notes-sub">
                  {profile.name}，你已经掌握 {mastered.length} 条，还有 {learning.length} 条在练。
                </div>
              </div>
              <button
                className="w4-btn is-primary"
                disabled={dueQueue.length === 0}
                onClick={() => setReviewOpen(true)}
              >
                <RotateCcw size={14} />
                开始复习
              </button>
            </div>
            <div className="w4-bar">
              <i style={{ width: `${progress}%` }} />
            </div>
            <div className="w4-notes-legend">
              <span>
                <i className="w4-dot" style={{ background: "var(--w-green)" }} />
                已掌握 {mastered.length}
              </span>
              <span>
                <i className="w4-dot" style={{ background: "#d8b25a" }} />
                在练 {learning.length}
              </span>
              <span style={{ marginLeft: "auto" }}>掌握 = 在聊天里自然用出来 3 次</span>
            </div>
          </div>

          <div className="w4-tabs">
            <button className={`w4-tab ${filter === "all" ? "on" : ""}`} onClick={() => setFilter("all")}>
              全部<span className="w4-tab-count">{expressions.length}</span>
            </button>
            <button className={`w4-tab ${filter === "learning" ? "on" : ""}`} onClick={() => setFilter("learning")}>
              在练<span className="w4-tab-count">{learning.length}</span>
            </button>
            <button className={`w4-tab ${filter === "mastered" ? "on" : ""}`} onClick={() => setFilter("mastered")}>
              已掌握<span className="w4-tab-count">{mastered.length}</span>
            </button>
          </div>

          {list.length === 0 ? (
            <Empty
              icon={<BookMarked size={24} strokeWidth={1.5} />}
              title="这里还是空的"
              text={
                <>
                  聊天时好友给你的建议，点「收进表达本」就会出现在这里。
                </>
              }
            />
          ) : (
            list.map((e) => {
              const source = friendById(e.sourceFriendId);
              return (
                <div key={e.id} className={`w4-note ${e.status === "mastered" ? "is-mastered" : ""}`}>
                  <div className="w4-note-main">
                    <div className="w4-note-tag-row">
                      {source ? <Avatar friend={source} size={16} round /> : null}
                      {source ? <span>{source.name} 教你的</span> : <span>手动收藏</span>}
                      <span>·</span>
                      <span>{e.createdAt}</span>
                      {e.status === "mastered" ? (
                        <span className="w4-note-mastered">
                          <Check size={10} strokeWidth={2.6} /> 已掌握
                        </span>
                      ) : null}
                    </div>
                    <div className="w4-note-better">{e.better}</div>
                    <div className="w4-note-original">{e.original}</div>
                    <div className="w4-note-why">{e.why}</div>
                  </div>
                  <div className="w4-note-side">
                    <span className="w4-mastery" title={`掌握度 ${e.mastery}/3`}>
                      {[0, 1, 2].map((i) => (
                        <i key={i} className={i < e.mastery ? "on" : ""} />
                      ))}
                    </span>
                    {e.status === "learning" ? (
                      <button className="w4-note-practice" onClick={() => setReviewOpen(true)}>
                        练一下
                      </button>
                    ) : (
                      <button
                        className="w4-note-practice"
                        style={{ background: "var(--w-fill)", color: "var(--w-text-2)" }}
                        onClick={() => toggleMastered(e.id)}
                      >
                        重新练
                      </button>
                    )}
                    <button
                      className="w4-note-remove"
                      aria-label="删除"
                      onClick={() => removeExpression(e.id)}
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                </div>
              );
            })
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

  if (!item) {
    return (
      <div className="w4-review">
        <div className="w4-review-head">
          <button className="w4-icon-btn" onClick={onClose} aria-label="关闭">
            <X size={17} />
          </button>
          <span style={{ fontSize: 13.5, fontWeight: 600 }}>今天练完了</span>
        </div>
        <div className="w4-review-body">
          <div className="w4-review-card">
            <div className="w4-review-done-icon">
              <CircleCheck size={30} strokeWidth={1.6} />
            </div>
            <div className="w4-review-q" style={{ fontSize: 18, marginTop: 10 }}>
              练了 {done} 条
            </div>
            <div className="w4-review-hint" style={{ marginTop: 6 }}>
              明天这个时候，它们会再出现一次。
            </div>
            <div className="w4-review-actions">
              <button className="w4-btn is-primary" onClick={onClose}>
                回到表达本
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="w4-review">
      <div className="w4-review-head">
        <button className="w4-icon-btn" onClick={onClose} aria-label="关闭">
          <X size={17} />
        </button>
        <span style={{ fontSize: 13.5, fontWeight: 600 }}>复习 · {index + 1}/{queue.length}</span>
        <div style={{ flex: 1 }} />
        <span style={{ fontSize: 11.5, color: "var(--w-text-3)" }}>已练 {done}</span>
      </div>
      <div className="w4-review-progress">
        <i style={{ width: `${(index / queue.length) * 100}%` }} />
      </div>
      <div className="w4-review-body">
        <div className="w4-review-card" key={item.id}>
          <div className="w4-review-label">你上次说</div>
          <div className="w4-review-q">“{item.original}”</div>
          <div className="w4-review-hint">更地道的说法是？</div>

          {revealed ? (
            <div className="w4-review-a">
              <div className="w4-review-a-main">{item.better}</div>
              <div className="w4-review-a-why">{item.why}</div>
              <div className="w4-review-actions">
                <button
                  className="w4-btn"
                  onClick={() => {
                    onAnswer(item.id, false);
                    setRevealed(false);
                    setIndex(index + 1);
                    setDone((d) => d + 1);
                  }}
                >
                  还不太熟
                </button>
                <button
                  className="w4-btn is-primary"
                  onClick={() => {
                    onAnswer(item.id, true);
                    setRevealed(false);
                    setIndex(index + 1);
                    setDone((d) => d + 1);
                  }}
                >
                  记住了
                </button>
              </div>
            </div>
          ) : (
            <div className="w4-review-actions">
              <button className="w4-btn is-primary" onClick={() => setRevealed(true)}>
                <Star size={14} />
                想一想，看答案
              </button>
            </div>
          )}
        </div>
      </div>
      <div className="w4-review-foot">在聊天里自然地用出来，比在这里背更有用。</div>
    </div>
  );
}
