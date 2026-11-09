"use client";

/**
 * 创建/编辑自定义角色。
 *
 * 结构化表单：名字、城市、职业、性格、风格、开场白、难度、场景。
 * 声线（Fish 音色）由后端按语言/性别自动分配，创建后不可手动更换。
 * 保存时后端自动生成 system_prompt（用户不用写 prompt）。
 */

import { useCallback, useEffect, useState } from "react";
import { Check, Loader2, Trash2, Volume2, X } from "lucide-react";

interface SceneOption {
  id: string;
  name: string;
  custom?: boolean;
}

interface PersonaForm {
  id: string;
  name: string;
  city: string;
  job: string;
  voice: string;
  gender: string;
  traits: string;
  style: string;
  opening_line: string;
  difficulty: number;
  scene_id: string;
  extra: string;
}

const EMPTY: PersonaForm = {
  id: "",
  name: "",
  city: "",
  job: "",
  voice: "en-GB-RyanNeural",
  gender: "male",
  traits: "",
  style: "",
  opening_line: "",
  difficulty: 3,
  scene_id: "",
  extra: "",
};

interface CreatePersonaDialogProps {
  open: boolean;
  onClose: () => void;
  onSaved?: () => void;
}

export function CreatePersonaDialog({ open, onClose, onSaved }: CreatePersonaDialogProps) {
  const [form, setForm] = useState<PersonaForm>(EMPTY);
  const [scenes, setScenes] = useState<SceneOption[]>([]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  /** 内联新建场景的草稿（下拉选「＋ 新建场景…」时展开） */
  const [sceneDraft, setSceneDraft] = useState({ title: "", text: "" });

  const load = useCallback(async () => {
    try {
      const [s, mus] = await Promise.all([
        fetch("/api/custom/scenes", { cache: "no-store" }).then((r) => r.json()),
        fetch("/api/custom/scene-templates", { cache: "no-store" }).then((r) => r.json()),
      ]);
      const custom = (s.scenes ?? []) as SceneOption[];
      const tpl = (mus.templates ?? []).map((t: { key: string; name: string }) => ({
        id: t.key,
        name: `${t.name}（模板）`,
      }));
      setScenes([...custom, ...tpl]);
    } catch {
      /* 忽略 */
    }
  }, []);

  useEffect(() => {
    if (open) {
      void load();
      setForm(EMPTY);
      setMsg("");
      setErr("");
    }
  }, [open, load]);

  const set = <K extends keyof PersonaForm>(k: K, v: PersonaForm[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  const save = async () => {
    if (!form.name.trim()) {
      setErr("请填写角色名字");
      return;
    }
    // 选了「＋ 新建场景…」→ 先建场景，拿到 id
    let sceneId = form.scene_id;
    if (sceneId === "__new__") {
      if (!sceneDraft.text.trim()) {
        setErr("请填写场景描述，或把场景改回「不指定」");
        return;
      }
      setBusy(true);
      setErr("");
      setMsg("");
      try {
        const sr = await fetch("/api/custom/scenes", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: (sceneDraft.title.trim() || "自定义场景").slice(0, 40),
            template: "blank",
            goal: sceneDraft.text.trim(),
          }),
        });
        const sj = (await sr.json()) as Record<string, unknown>;
        if (!sr.ok) throw new Error(typeof sj.detail === "string" ? sj.detail : `HTTP ${sr.status}`);
        const sc = (sj.scene ?? sj) as Record<string, unknown>;
        sceneId = typeof sc.id === "string" ? sc.id : "";
        if (!sceneId) throw new Error("场景创建成功但没拿到 id");
      } catch (e) {
        setErr(`场景创建失败：${e instanceof Error ? e.message : String(e)}`);
        setBusy(false);
        return;
      }
    }

    setBusy(true);
    setErr("");
    setMsg("");
    try {
      const r = await fetch("/api/custom/personas", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...form, name: form.name.trim(), scene_id: sceneId }),
      });
      const j = (await r.json()) as Record<string, unknown>;
      if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : `HTTP ${r.status}`);
      const p = j.persona as Record<string, unknown> | undefined;
      setMsg(`角色「${p?.name ?? form.name}」已创建`);
      onSaved?.();
      setTimeout(onClose, 900);
    } catch (e) {
      setErr(`保存失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true">
      <button type="button" aria-label="关闭" onClick={onClose} className="absolute inset-0 bg-black/75" />
      <div className="relative flex max-h-[90vh] w-full max-w-[700px] flex-col overflow-hidden rounded-[3px] border border-[color:var(--line-strong)] bg-[color:var(--surface)] shadow-2xl">
        {/* 头部 */}
        <div className="flex shrink-0 items-center gap-2.5 border-b border-[color:var(--line)] px-4 py-3">
          <Volume2 size={14} className="text-[color:var(--accent)]" />
          <div className="min-w-0 flex-1">
            <h2 className="text-[13px] font-semibold text-[color:var(--text)]">设计一个新角色</h2>
            <p className="mt-0.5 text-[9.5px] uppercase tracking-[0.14em] text-[color:var(--text-faint)]">
              Custom Persona · 填表即可，提示词自动生成
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="flex h-6 w-6 items-center justify-center rounded-[2px] text-[color:var(--text-faint)] hover:text-[color:var(--text)]"
          >
            <X size={14} />
          </button>
        </div>

        {/* 表单 */}
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-4 py-3.5">
          {err && (
            <div className="rounded-[2px] border border-[color:var(--accent-danger)] px-2.5 py-1.5 text-[10.5px] text-[color:var(--accent-danger)]">
              {err}
            </div>
          )}
          {msg && (
            <div className="flex items-center gap-1.5 rounded-[2px] border border-[color:var(--accent-ok)] px-2.5 py-1.5 text-[10.5px] text-[color:var(--accent-ok)]">
              <Check size={11} /> {msg}
            </div>
          )}

          {/* 基本信息 */}
          <div className="grid grid-cols-2 gap-2.5">
            <Field label="名字 *">
              <input
                value={form.name}
                onChange={(e) => set("name", e.target.value)}
                placeholder="如 Chidi"
                maxLength={40}
                className={INPUT}
              />
            </Field>
            <Field label="性别">
              <select value={form.gender} onChange={(e) => set("gender", e.target.value)} className={INPUT}>
                <option value="male">男</option>
                <option value="female">女</option>
              </select>
            </Field>
            <Field label="城市">
              <input
                value={form.city}
                onChange={(e) => set("city", e.target.value)}
                placeholder="如 Berlin"
                maxLength={40}
                className={INPUT}
              />
            </Field>
            <Field label="职业">
              <input
                value={form.job}
                onChange={(e) => set("job", e.target.value)}
                placeholder="如 创始人"
                maxLength={60}
                className={INPUT}
              />
            </Field>
          </div>

          {/* 声线：由系统自动分配（Fish 音色库），不提供手动选择 */}
          <Field label="声线">
            <p className="rounded-[2px] border border-[color:var(--line)] bg-[color:var(--surface-2)] px-2.5 py-1.5 text-[10.5px] leading-relaxed text-[color:var(--text-dim)]">
              声线由系统自动分配（Fish 音色库），创建后不可手动更换。
            </p>
          </Field>

          {/* 性格与风格 */}
          <Field label="性格（逗号分隔）">
            <input
              value={form.traits}
              onChange={(e) => set("traits", e.target.value)}
              placeholder="直接, 好奇, 要求高"
              maxLength={200}
              className={INPUT}
            />
          </Field>
          <Field label="说话风格">
            <input
              value={form.style}
              onChange={(e) => set("style", e.target.value)}
              placeholder="说话快，喜欢追问细节和数据"
              maxLength={300}
              className={INPUT}
            />
          </Field>

          {/* 开场白 */}
          <Field label="开场白（第一句话）">
            <textarea
              value={form.opening_line}
              onChange={(e) => set("opening_line", e.target.value)}
              placeholder="Come in, sit down. So — tell me about something you built from scratch."
              maxLength={300}
              rows={2}
              className={`${INPUT} resize-none`}
            />
          </Field>

          {/* 场景 + 难度 */}
          <div className="grid grid-cols-2 gap-2.5">
            <Field label="场景">
              <select value={form.scene_id} onChange={(e) => set("scene_id", e.target.value)} className={INPUT}>
                <option value="">（不指定）</option>
                <option value="__new__">＋ 新建场景…</option>
                {scenes.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={`难度 ${form.difficulty}/5`}>
              <input
                type="range"
                min={1}
                max={5}
                value={form.difficulty}
                onChange={(e) => set("difficulty", Number(e.target.value))}
                className="w-full accent-[color:var(--accent)]"
              />
            </Field>
          </div>

          {form.scene_id === "__new__" && (
            <div className="rounded-[8px] border border-[color:var(--line)] bg-[color:var(--panel)]/60 p-2.5">
              <p className="mb-2 text-[10.5px] leading-[15px] text-[color:var(--muted)]">
                场景会写进这个角色的提示词，聊这个角色时就自动生效。
              </p>
              <Field label="场景标题">
                <input
                  value={sceneDraft.title}
                  onChange={(e) => setSceneDraft((d) => ({ ...d, title: e.target.value }))}
                  placeholder="占座的 teenagers"
                  maxLength={40}
                  className={INPUT}
                />
              </Field>
              <div className="mt-2">
                <Field label="场景描述 *">
                  <textarea
                    value={sceneDraft.text}
                    onChange={(e) => setSceneDraft((d) => ({ ...d, text: e.target.value }))}
                    placeholder="你在星巴克，两个十几岁的少年占了你的座位，还在大声外放音乐。你想客气但坚定地把座位要回来。"
                    maxLength={400}
                    rows={3}
                    className={`${INPUT} resize-none`}
                  />
                </Field>
              </div>
            </div>
          )}

          <Field label="额外设定（可选）">
            <textarea
              value={form.extra}
              onChange={(e) => set("extra", e.target.value)}
              placeholder="会说一点德语，偶尔夹德语词"
              maxLength={500}
              rows={2}
              className={`${INPUT} resize-none`}
            />
          </Field>

          <p className="text-[9.5px] leading-relaxed text-[color:var(--text-faint)]">
            提示词会自动生成：口音通过<b className="text-[color:var(--text-dim)]">行为描述</b>
            （语速、节奏、用词）体现，不会用假拼写冒充口音。出错时对方会用「重述」自然示范正确说法。
          </p>
        </div>

        {/* 底部 */}
        <div className="flex shrink-0 items-center justify-between gap-2 border-t border-[color:var(--line)] px-4 py-2.5">
          <button
            type="button"
            onClick={() => {
              setForm(EMPTY);
              setErr("");
              setMsg("");
            }}
            className="flex items-center gap-1 rounded-[2px] px-2 py-1.5 text-[10.5px] text-[color:var(--text-faint)] hover:text-[color:var(--accent-danger)]"
          >
            <Trash2 size={11} />
            清空
          </button>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-[2px] border border-[color:var(--line-strong)] px-3 py-1.5 text-[11px] text-[color:var(--text-dim)] hover:text-[color:var(--text)]"
            >
              取消
            </button>
            <button
              type="button"
              onClick={() => void save()}
              disabled={busy || !form.name.trim()}
              className="flex items-center gap-1.5 rounded-[2px] border border-[color:var(--accent-line)] bg-[color:var(--accent)]/20 px-3.5 py-1.5 text-[11px] font-medium text-[color:var(--accent)] transition-colors hover:bg-[color:var(--accent)]/30 disabled:opacity-40"
            >
              {busy ? <Loader2 size={11} className="animate-spin" /> : <Check size={11} />}
              创建角色
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

const INPUT =
  "w-full rounded-[2px] border border-[color:var(--line-strong)] bg-[color:var(--surface-2)] px-2 py-1.5 text-[11px] text-[color:var(--text)] placeholder:text-[color:var(--text-faint)] focus:border-[color:var(--accent-line)] focus:outline-none";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="mb-1 block text-[9.5px] font-medium uppercase tracking-[0.1em] text-[color:var(--text-faint)]">
        {label}
      </label>
      {children}
    </div>
  );
}
