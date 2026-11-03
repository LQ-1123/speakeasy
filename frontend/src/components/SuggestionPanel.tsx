"use client";

import { Lightbulb, ArrowRight, Wand2 } from "lucide-react";

import { REGISTER_LABEL } from "@/lib/fallback";
import type { SuggestionCard } from "@/lib/types";

interface SuggestionPanelProps {
  suggestions: SuggestionCard[];
  onRequest?: () => void;
  connected: boolean;
}

/**
 * 右栏 · 润色建议
 *
 * 卡片按到达顺序追加（`suggestion` 异步消息，可能晚于 assistant_done）。
 * 每条展示：original（红/删除线）→ better（绿/加粗）→ why（中文说明）+ register 标签。
 * "未收起"动画：新卡片从右侧滑入并带一圈临时高亮。
 */
export function SuggestionPanel({ suggestions, onRequest, connected }: SuggestionPanelProps) {
  // 最新的在最上面（面板阅读顺序从上往下 = 从新到旧）
  const ordered = [...suggestions].reverse();

  return (
    <aside className="flex h-full min-h-0 flex-col bg-[color:var(--surface)]">
      <div className="flex items-baseline justify-between border-b border-[color:var(--line)] px-3.5 py-2.5">
        <div className="flex items-center gap-1.5">
          <Lightbulb size={12} className="text-[color:var(--accent)]" />
          <span className="panel-label">润色建议</span>
        </div>
        <div className="flex items-center gap-2">
          {suggestions.length > 0 && (
            <span className="tnum text-[10px] text-[color:var(--text-faint)]">{suggestions.length}</span>
          )}
          <button
            type="button"
            onClick={onRequest}
            disabled={!connected}
            title="对最近一句请求润色建议"
            className="flex h-5 w-5 items-center justify-center rounded-[2px] text-[color:var(--text-faint)] transition-colors hover:text-[color:var(--accent)] disabled:opacity-40 disabled:hover:text-[color:var(--text-faint)]"
          >
            <Wand2 size={11} />
          </button>
        </div>
      </div>

      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto p-2.5">
        {ordered.length === 0 ? (
          <div className="flex flex-col items-center px-3 py-12 text-center">
            <div className="mb-3 flex h-9 w-9 items-center justify-center rounded-sm border border-[color:var(--line-strong)] bg-[color:var(--surface-2)]">
              <Lightbulb size={15} className="text-[color:var(--text-faint)]" />
            </div>
            <p className="text-[11px] leading-relaxed text-[color:var(--text-faint)]">
              {connected
                ? "说完一句后，后台会异步生成润色建议。也可以点右上角图标主动请求。"
                : "连接后端后，每轮对话结束会自动推送润色建议。"}
            </p>
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            {ordered.map((card, index) => (
              <SuggestionItem key={card.id} card={card} latest={index === 0} />
            ))}
          </div>
        )}
      </div>
    </aside>
  );
}

function SuggestionItem({ card, latest }: { card: SuggestionCard; latest: boolean }) {
  return (
    <article
      className={`anim-slide rounded-sm border bg-[color:var(--surface-2)] p-2.5 ${
        latest
          ? "border-[color:var(--accent-line)] shadow-[0_0_0_1px_var(--accent-dim)]"
          : "border-[color:var(--line-strong)]"
      }`}
    >
      {/* register 标签 */}
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="tnum text-[9px] uppercase tracking-[0.14em] text-[color:var(--text-faint)]">
          #{card.turnId >= 0 ? card.turnId : "—"}
        </span>
        <span className="rounded-[2px] border border-[color:var(--line-strong)] px-1.5 py-px text-[9px] text-[color:var(--text-dim)]">
          {REGISTER_LABEL[card.register] ?? card.register}
        </span>
      </div>

      {/* original：红 + 删除线 */}
      <p className="text-[12px] leading-relaxed text-[color:var(--accent-danger)]/90 line-through decoration-[color:var(--accent-danger)]/50">
        {card.original}
      </p>

      {/* 箭头 */}
      <div className="my-1 flex items-center gap-1 pl-0.5">
        <ArrowRight size={10} className="text-[color:var(--text-faint)]" />
      </div>

      {/* better：绿 + 加粗 */}
      <p className="text-[13px] font-semibold leading-relaxed text-[color:var(--accent-ok)]">
        {card.better}
      </p>

      {/* why：中文说明 */}
      {card.why && (
        <p className="mt-2 border-t border-[color:var(--line)] pt-2 text-[10.5px] leading-relaxed text-[color:var(--text-dim)]">
          {card.why}
        </p>
      )}
    </article>
  );
}
