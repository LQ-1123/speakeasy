"use client";

/**
 * 口音市场：浏览全部可用口音，试听，给角色套用。
 *
 * 数据来自 /api/market/*，共 50 个声线（36 官方变体 + 14 外语兜底）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertCircle,
  Check,
  Loader2,
  Play,
  Search,
  Volume2,
  X,
} from "lucide-react";

interface AccentItem {
  voice: string;
  locale: string;
  label: string;
  label_zh: string;
  gender: string;
  kind: "official" | "foreign_fallback";
  kind_label: string;
  region_zh: string;
  region_en: string;
  flag: string;
  vibe: string;
  suggested_roles: string[];
}

interface RegionItem {
  locale: string;
  label_zh: string;
  label_en: string;
  flag: string;
  kind: string;
  vibe: string;
  voice_count: number;
}

interface PersonaRow {
  id: string;
  name: string;
  accent: string;
  flag: string;
}

interface AccentMarketDialogProps {
  open: boolean;
  onClose: () => void;
  /** 当前选中的角色（用于"应用到此角色"） */
  personaId: string;
  personaName: string;
  onApplied?: () => void;
}

type KindFilter = "all" | "official" | "foreign_fallback";

export function AccentMarketDialog({
  open,
  onClose,
  personaId,
  personaName,
  onApplied,
}: AccentMarketDialogProps) {
  const [accents, setAccents] = useState<AccentItem[]>([]);
  const [regions, setRegions] = useState<RegionItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [kind, setKind] = useState<KindFilter>("all");
  const [q, setQ] = useState("");
  const [activeLocale, setActiveLocale] = useState<string>("");
  const [playing, setPlaying] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [personas, setPersonas] = useState<PersonaRow[]>([]);
  const [target, setTarget] = useState(personaId);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [a, r, p] = await Promise.all([
        fetch("/api/market/accents", { cache: "no-store" }).then((x) => x.json()),
        fetch("/api/market/regions", { cache: "no-store" }).then((x) => x.json()),
        fetch("/api/personas", { cache: "no-store" })
          .then((x) => x.json())
          .catch(() => ({ personas: [] })),
      ]);
      setAccents(a.accents ?? []);
      setRegions(r.regions ?? []);
      const rows = (p.personas ?? p) as PersonaRow[];
      setPersonas(Array.isArray(rows) ? rows : []);
    } catch {
      setMsg("加载口音市场失败");
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  useEffect(() => {
    if (open) setTarget(personaId);
  }, [open, personaId]);

  /** 试听 */
  const preview = async (voice: string) => {
    setPlaying(voice);
    setMsg("");
    try {
      const r = await fetch("/api/market/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          voice,
          text: "Right then, that's a flat white for you. Anything else?",
        }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const el = audioRef.current;
      if (el) {
        el.src = url;
        el.onended = () => setPlaying("");
        void el.play().catch(() => setPlaying(""));
      }
    } catch (e) {
      setMsg(`试听失败：${e instanceof Error ? e.message : String(e)}`);
      setPlaying("");
    }
  };

  /** 应用到角色 */
  const apply = async (voice: string) => {
    if (!target) {
      setMsg("请先选择要应用的角色");
      return;
    }
    setBusy(true);
    setMsg("");
    try {
      const r = await fetch("/api/market/assign", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ persona_id: target, voice }),
      });
      const j = (await r.json()) as Record<string, unknown>;
      if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : `HTTP ${r.status}`);
      setMsg(String(j.message ?? "已应用"));
      onApplied?.();
    } catch (e) {
      setMsg(`应用失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  const filtered = useMemo(() => {
    let rows = accents;
    if (kind !== "all") rows = rows.filter((a) => a.kind === kind);
    if (activeLocale) rows = rows.filter((a) => a.locale === activeLocale);
    const s = q.trim().toLowerCase();
    if (s) {
      rows = rows.filter(
        (a) =>
          a.label.toLowerCase().includes(s) ||
          a.voice.toLowerCase().includes(s) ||
          a.region_zh.includes(s) ||
          a.region_en.toLowerCase().includes(s) ||
          a.vibe.includes(s),
      );
    }
    return rows;
  }, [accents, kind, activeLocale, q]);

  const grouped = useMemo(() => {
    const m = new Map<string, AccentItem[]>();
    for (const a of filtered) {
      const arr = m.get(a.locale) ?? [];
      arr.push(a);
      m.set(a.locale, arr);
    }
    return Array.from(m.entries()).sort(([x], [y]) => {
      // 官方变体排前
      const xo = regions.find((r) => r.locale === x)?.kind === "official" ? 0 : 1;
      const yo = regions.find((r) => r.locale === y)?.kind === "official" ? 0 : 1;
      return xo - yo || x.localeCompare(y);
    });
  }, [filtered, regions]);

  if (!open) return null;

  const visibleRegions = regions.filter(
    (r) => kind === "all" || r.kind === kind || kind === "foreign_fallback",
  );

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true">
      <button
        type="button"
        aria-label="关闭"
        onClick={onClose}
        className="absolute inset-0 bg-black/75 backdrop-blur-[2px]"
      />
      <div className="relative flex h-[88vh] w-full max-w-[1000px] flex-col overflow-hidden rounded-[3px] border border-[color:var(--line-strong)] bg-[color:var(--surface)] shadow-2xl">
        {/* 头部 */}
        <div className="flex shrink-0 items-center gap-3 border-b border-[color:var(--line)] px-5 py-3">
          <Volume2 size={16} className="text-[color:var(--accent)]" />
          <div className="min-w-0 flex-1">
            <h2 className="text-[14px] font-semibold text-[color:var(--text)]">
              口音市场
            </h2>
            <p className="mt-0.5 text-[10px] uppercase tracking-[0.14em] text-[color:var(--text-faint)]">
              {accents.length} 个声线 · {regions.length} 个地区 · 官方变体优先
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="flex h-7 w-7 items-center justify-center rounded-[2px] text-[color:var(--text-faint)] hover:text-[color:var(--text)]"
          >
            <X size={16} />
          </button>
        </div>

        {/* 工具栏 */}
        <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-[color:var(--line)] px-5 py-2.5">
          <div className="relative">
            <Search
              size={12}
              className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-[color:var(--text-faint)]"
            />
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="搜索地区 / 声线 / 感觉"
              className="w-[210px] rounded-[2px] border border-[color:var(--line-strong)] bg-[color:var(--surface-2)] py-1.5 pl-7 pr-2 text-[11px] text-[color:var(--text)] placeholder:text-[color:var(--text-faint)] focus:border-[color:var(--accent-line)] focus:outline-none"
            />
          </div>

          <div className="flex overflow-hidden rounded-[2px] border border-[color:var(--line-strong)]">
            {(
              [
                ["all", "全部"],
                ["official", "官方变体"],
                ["foreign_fallback", "外语腔"],
              ] as Array<[KindFilter, string]>
            ).map(([k, label]) => (
              <button
                key={k}
                type="button"
                onClick={() => {
                  setKind(k);
                  setActiveLocale("");
                }}
                className={`px-2.5 py-1.5 text-[10.5px] transition-colors ${
                  kind === k
                    ? "bg-[color:var(--accent)]/20 text-[color:var(--accent)]"
                    : "bg-[color:var(--surface-2)] text-[color:var(--text-dim)] hover:text-[color:var(--text)]"
                }`}
              >
                {label}
              </button>
            ))}
          </div>

          <div className="ml-auto flex items-center gap-1.5">
            <span className="text-[10px] text-[color:var(--text-faint)]">应用到</span>
            <select
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              className="rounded-[2px] border border-[color:var(--line-strong)] bg-[color:var(--surface-2)] px-2 py-1.5 text-[11px] text-[color:var(--text)] focus:outline-none"
            >
              {personas.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.flag} {p.name}
                </option>
              ))}
            </select>
          </div>
        </div>

        {/* 状态条 */}
        {msg && (
          <div className="shrink-0 border-b border-[color:var(--line)] px-5 py-1.5">
            <p className="flex items-center gap-1.5 text-[10.5px] text-[color:var(--accent-ok)]">
              <Check size={11} />
              {msg}
            </p>
          </div>
        )}

        {/* 主体 */}
        <div className="flex min-h-0 flex-1">
          {/* 左侧：地区 */}
          <div className="w-[170px] shrink-0 overflow-y-auto border-r border-[color:var(--line)] px-2 py-2">
            <button
              type="button"
              onClick={() => setActiveLocale("")}
              className={`mb-1 w-full rounded-[2px] px-2 py-1.5 text-left text-[10.5px] transition-colors ${
                activeLocale === ""
                  ? "bg-[color:var(--accent)]/15 text-[color:var(--accent)]"
                  : "text-[color:var(--text-dim)] hover:bg-[color:var(--surface-2)]"
              }`}
            >
              全部地区 ({accents.length})
            </button>
            {visibleRegions.map((r) => (
              <button
                key={r.locale}
                type="button"
                onClick={() => setActiveLocale(r.locale)}
                title={r.vibe}
                className={`mb-0.5 flex w-full items-center gap-1.5 rounded-[2px] px-2 py-1.5 text-left text-[10.5px] transition-colors ${
                  activeLocale === r.locale
                    ? "bg-[color:var(--accent)]/15 text-[color:var(--accent)]"
                    : "text-[color:var(--text-dim)] hover:bg-[color:var(--surface-2)]"
                }`}
              >
                <span>{r.flag}</span>
                <span className="min-w-0 flex-1 truncate">{r.label_zh}</span>
                <span className="shrink-0 text-[9px] text-[color:var(--text-faint)]">
                  {r.voice_count}
                </span>
                {r.kind !== "official" && (
                  <span className="shrink-0 text-[8px] text-[color:var(--accent-warn,#d97706)]">
                    兜底
                  </span>
                )}
              </button>
            ))}
          </div>

          {/* 右侧：声线列表 */}
          <div className="min-w-0 flex-1 overflow-y-auto px-4 py-3">
            {loading ? (
              <div className="flex items-center justify-center gap-2 py-16 text-[11px] text-[color:var(--text-faint)]">
                <Loader2 size={13} className="animate-spin" />
                加载口音…
              </div>
            ) : filtered.length === 0 ? (
              <div className="flex flex-col items-center gap-2 py-16 text-[11px] text-[color:var(--text-faint)]">
                <AlertCircle size={16} />
                没有匹配的口音
              </div>
            ) : (
              <div className="space-y-4">
                {grouped.map(([locale, rows]) => {
                  const meta = regions.find((r) => r.locale === locale);
                  return (
                    <div key={locale}>
                      <div className="mb-1.5 flex items-baseline gap-2">
                        <span className="text-[11px] font-medium text-[color:var(--text)]">
                          {meta?.flag} {meta?.label_zh ?? locale}
                        </span>
                        <span className="text-[9px] text-[color:var(--text-faint)]">
                          {meta?.label_en} · {locale}
                        </span>
                        {meta && meta.kind !== "official" && (
                          <span className="rounded-[2px] border border-[color:var(--accent-warn,#d97706)] px-1 text-[8px] text-[color:var(--accent-warn,#d97706)]">
                            外语兜底
                          </span>
                        )}
                      </div>
                      <div className="grid grid-cols-1 gap-1.5 lg:grid-cols-2">
                        {rows.map((a) => (
                          <div
                            key={a.voice}
                            className="group flex items-center gap-2 rounded-[2px] border border-[color:var(--line)] bg-[color:var(--surface-2)] px-2.5 py-1.5 transition-colors hover:border-[color:var(--accent-line)]"
                          >
                            <div className="min-w-0 flex-1">
                              <div className="flex items-center gap-1.5">
                                <span className="truncate text-[10.5px] text-[color:var(--text)]">
                                  {a.label_zh}
                                </span>
                                {a.kind !== "official" && (
                                  <span className="shrink-0 text-[8px] text-[color:var(--accent-warn,#d97706)]">
                                    兜底
                                  </span>
                                )}
                              </div>
                              <div className="truncate text-[9px] text-[color:var(--text-faint)]">
                                {a.vibe}
                              </div>
                            </div>
                            <button
                              type="button"
                              onClick={() => void preview(a.voice)}
                              disabled={playing === a.voice}
                              title="试听"
                              className="shrink-0 text-[color:var(--text-faint)] transition-colors hover:text-[color:var(--accent)] disabled:text-[color:var(--accent)]"
                            >
                              {playing === a.voice ? (
                                <Loader2 size={11} className="animate-spin" />
                              ) : (
                                <Play size={11} />
                              )}
                            </button>
                            <button
                              type="button"
                              disabled={busy || !target}
                              onClick={() => void apply(a.voice)}
                              title={`应用到 ${personaName || "选中角色"}`}
                              className="shrink-0 text-[color:var(--text-faint)] transition-colors hover:text-[color:var(--accent-ok)] disabled:opacity-30"
                            >
                              <Check size={11} />
                            </button>
                          </div>
                        ))}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>

        <div className="shrink-0 border-t border-[color:var(--line)] px-5 py-2">
          <p className="text-[9.5px] leading-relaxed text-[color:var(--text-faint)]">
            <b className="text-[color:var(--accent-ok)]">官方英语变体</b>
            是真实地区口音（100% 自然）。
            <b className="text-[color:var(--accent-warn,#d97706)]">外语兜底</b>
            用于没有官方英语变体的地区（意大利/法国/德国/日本/中国），口音浓但个别英文单词可能读不准。
          </p>
        </div>

        <audio ref={audioRef} className="hidden" />
      </div>
    </div>
  );
}
