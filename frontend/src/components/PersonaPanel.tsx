"use client";

import { Check, Loader2, MapPin, Mic2, Sparkles, Target, Timer, Volume2, Wrench } from "lucide-react";

import { DIFFICULTY_COLOR, DIFFICULTY_LABEL } from "@/lib/fallback";
import type { ConversationMode, PersonaPublic } from "@/lib/types";

interface PersonaPanelProps {
  personas: PersonaPublic[];
  modes: ConversationMode[];
  activePersonaId: string;
  activeMode: string;
  onSelectPersona: (id: string) => void;
  onSelectMode: (id: string) => void;
  /** 打开「用你的声音说话」上传面板 */
  onOpenVoiceClone: () => void;
}

const MODE_ICON: Record<string, React.ReactNode> = {
  free: <Sparkles size={13} />,
  task: <Target size={13} />,
  drill: <Wrench size={13} />,
};

/**
 * 左栏 · 角色面板
 *
 * 设计取舍：不做圆角大卡片堆叠。每个角色是一行"条目"，
 * 用左侧 2px 竖线（.selected-rail）+ 背景微差表达选中，
 * 信息密度按工程面板组织：flag / 名字 / 城市 / 难度点 / traits。
 */
export function PersonaPanel({
  personas,
  modes,
  activePersonaId,
  activeMode,
  onSelectPersona,
  onSelectMode,
  onOpenVoiceClone,
}: PersonaPanelProps) {
  return (
    <aside className="flex h-full min-h-0 flex-col bg-[color:var(--surface)]">
      {/* ---- 声音克隆入口 ---- */}
      <div className="shrink-0 border-b border-[color:var(--line)] px-3 py-2.5">
        <button
          type="button"
          onClick={onOpenVoiceClone}
          className="flex w-full items-center gap-2 rounded-[2px] border border-[color:var(--line-strong)] bg-[color:var(--surface-2)] px-2.5 py-2 text-left transition-colors hover:border-[color:var(--accent-line)] hover:bg-[color:var(--accent-dim)]"
        >
          <Volume2 size={13} className="shrink-0 text-[color:var(--accent)]" />
          <div className="min-w-0 flex-1">
            <div className="text-[11px] font-medium text-[color:var(--text)]">
              用你的声音说话
            </div>
            <div className="mt-0.5 text-[9px] leading-tight text-[color:var(--text-faint)]">
              上传 30s–1min 录音，克隆音色
            </div>
          </div>
        </button>
      </div>

      {/* ---- 角色列表 */}
      <div className="flex items-baseline justify-between border-b border-[color:var(--line)] px-3.5 py-2.5">
        <span className="panel-label">对话角色</span>
        <span className="tnum text-[10px] text-[color:var(--text-faint)]">{personas.length}</span>
      </div>

      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
        <ul>
          {personas.map((persona) => {
            const selected = persona.id === activePersonaId;
            const diff = persona.difficulty ?? "medium";
            return (
              <li key={persona.id}>
                <button
                  type="button"
                  onClick={() => onSelectPersona(persona.id)}
                  aria-pressed={selected}
                  className={`group relative w-full border-b border-[color:var(--line)] px-3.5 py-2.5 text-left transition-colors ${
                    selected
                      ? "selected-rail bg-[color:var(--accent-dim)]/40"
                      : "hover:bg-[color:var(--surface-2)]"
                  }`}
                >
                  <div className="flex items-start gap-2.5">
                    {/* flag */}
                    <span className="mt-0.5 text-[17px] leading-none" aria-hidden>
                      {persona.flag ?? "🏳️"}
                    </span>

                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5">
                        <span
                          className={`truncate text-[12.5px] font-semibold ${
                            selected ? "text-[color:var(--accent)]" : "text-[color:var(--text)]"
                          }`}
                        >
                          {persona.name}
                        </span>
                        {selected && (
                          <Check size={11} className="shrink-0 text-[color:var(--accent)]" strokeWidth={3} />
                        )}
                      </div>

                      <div className="mt-0.5 truncate text-[10.5px] text-[color:var(--text-dim)]">
                        {persona.title ?? persona.accent_short ?? ""}
                      </div>

                      <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1">
                        {/* 城市 */}
                        <span className="tnum flex items-center gap-0.5 text-[9.5px] uppercase tracking-wider text-[color:var(--text-faint)]">
                          <MapPin size={8.5} />
                          {persona.city ?? "—"}
                        </span>

                        {/* 难度色点 */}
                        <span className="flex items-center gap-1">
                          <span
                            className="h-1.5 w-1.5 rounded-full"
                            style={{ background: DIFFICULTY_COLOR[diff] ?? "var(--text-faint)" }}
                            aria-hidden
                          />
                          <span
                            className="text-[9.5px]"
                            style={{ color: DIFFICULTY_COLOR[diff] ?? "var(--text-faint)" }}
                          >
                            {DIFFICULTY_LABEL[diff] ?? diff}
                          </span>
                        </span>

                        {/* 口音简称 */}
                        {persona.accent_short && (
                          <span className="flex items-center gap-0.5 text-[9.5px] text-[color:var(--text-faint)]">
                            <Mic2 size={8.5} />
                            {persona.accent_short}
                          </span>
                        )}
                      </div>

                      {/* traits 标签 */}
                      {persona.traits && persona.traits.length > 0 && (
                        <div className="mt-1.5 flex flex-wrap gap-1">
                          {persona.traits.slice(0, 4).map((trait) => (
                            <span
                              key={trait}
                              className="rounded-[2px] border border-[color:var(--line-strong)] px-1 py-px text-[9px] text-[color:var(--text-faint)]"
                            >
                              {trait}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                </button>
              </li>
            );
          })}
        </ul>
      </div>

      {/* ---- 对话模式 */}
      <div className="shrink-0 border-t border-[color:var(--line-strong)]">
        <div className="flex items-baseline justify-between border-b border-[color:var(--line)] px-3.5 py-2.5">
          <span className="panel-label">对话模式</span>
          <span className="flex items-center gap-1 text-[9.5px] text-[color:var(--text-faint)]">
            <Timer size={9} />
            {modes.length} 种
          </span>
        </div>
        <div className="p-2">
          {modes.map((mode) => {
            const selected = mode.id === activeMode;
            return (
              <button
                key={mode.id}
                type="button"
                onClick={() => onSelectMode(mode.id)}
                aria-pressed={selected}
                title={mode.description}
                className={`mb-1 flex w-full items-start gap-2 rounded-[2px] border px-2.5 py-2 text-left transition-all last:mb-0 ${
                  selected
                    ? "border-[color:var(--accent-line)] bg-[color:var(--accent-dim)]"
                    : "border-transparent hover:border-[color:var(--line-strong)] hover:bg-[color:var(--surface-2)]"
                }`}
              >
                <span
                  className={`mt-px shrink-0 ${selected ? "text-[color:var(--accent)]" : "text-[color:var(--text-faint)]"}`}
                >
                  {MODE_ICON[mode.id] ?? <Sparkles size={13} />}
                </span>
                <span className="min-w-0 flex-1">
                  <span
                    className={`block text-[11.5px] font-medium ${
                      selected ? "text-[color:var(--accent)]" : "text-[color:var(--text-dim)]"
                    }`}
                  >
                    {mode.name}
                  </span>
                  {mode.description && (
                    <span className="mt-0.5 block text-[9.5px] leading-snug text-[color:var(--text-faint)]">
                      {mode.description}
                    </span>
                  )}
                </span>
                {selected && <Check size={11} className="mt-0.5 shrink-0 text-[color:var(--accent)]" strokeWidth={3} />}
              </button>
            );
          })}
        </div>
      </div>
    </aside>
  );
}

/** 角色切换时的短暂"挂起"指示，用于面板头部 */
export function PersonaPendingHint({ visible }: { visible: boolean }) {
  if (!visible) return null;
  return <Loader2 size={11} className="animate-spin text-[color:var(--accent)]" />;
}
