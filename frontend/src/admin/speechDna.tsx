"use client";

/**
 * 表达 DNA 结构化编辑器。
 *
 * 为什么要有它
 * ------------
 * 角色工坊原来把 `speech_dna` 当裸 JSON 文本框（`<TextArea mono rows={4}>`）。
 * 结果是：运营要手写 13 个英文 key 和英文枚举值，还得记住每个字段允许哪些值——
 * 实际上没人改得动，这个字段等于只读。而它恰恰是决定"角色说话像不像一个人"的
 * 核心参数（语气、直白度、幽默方式、emoji 频率…）。
 *
 * 三个设计决定
 * ------------
 * 1. **字段与可选值来自词库（`data/lexicons/dna.json`），不在这里硬编码。**
 *    词库是唯一真相：DnaSection 加了新字段/新取值，这里自动出现。
 *    硬编码一份必然漂移，而且漂移时表现为"界面上没有这个选项"，极难发现。
 * 2. **中文名是纯展示层。** 词库历史上没持久化过 label，所以中文文案在本文件里，
 *    找不到映射时**退回英文 key 原样显示**（不吞数据，也不假装翻译过）。
 * 3. **滑杆 + 分段按钮并存。** 枚举值自带 `trait`（0..1）且有语义顺序
 *    （clipped→measured→flowing→fragmented），滑杆能表达"往哪边调"；
 *    但滑杆不适合精确点选，所以同时给一排分段按钮。
 */

import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { apiGet } from "./api";
import { adminKeys } from "./queryKeys";
import { Badge, Field, Slider, TagCloud, TextInput } from "./ui";

/* -------------------------------------------------------------------------- */
/* 词库形状（与 backend/app/v3/lexicon.py 的 _validate_dna 对齐）              */
/* -------------------------------------------------------------------------- */

interface DnaValueMeta {
  enabled?: boolean;
  trait?: number;
}

interface DnaField {
  values?: Record<string, DnaValueMeta>;
  default?: string;
}

interface DnaLexicon {
  ok?: boolean;
  data?: { version?: number; fields?: Record<string, DnaField> };
}

/* -------------------------------------------------------------------------- */
/* 中文展示层                                                                  */
/* -------------------------------------------------------------------------- */

/** 字段中文名。键必须与 dna.json 的字段 key 完全一致。 */
const FIELD_LABEL: Record<string, string> = {
  sentence_rhythm: "断句节奏",
  directness: "直白程度",
  humor_style: "幽默方式",
  emotional_expression: "情绪外露",
  vocabulary_style: "用词风格",
  slang_usage: "俚语频率",
  emoji_usage: "表情符号",
  verbosity: "话多话少",
  conversation_initiative: "主动发起",
  self_disclosure: "自我暴露",
  social_distance: "社交距离",
  disagreement_style: "意见不同时",
  topic_transition: "换话题方式",
};

const FIELD_HINT: Record<string, string> = {
  sentence_rhythm: "句子长短与停顿的松紧，越往右越跳脱",
  directness: "越往右越不加修饰，越往左越绕",
  humor_style: "开玩笑的方式",
  emotional_expression: "情绪写在脸上的程度",
  vocabulary_style: "选词的调性",
  slang_usage: "口语化/网络用语的比例",
  emoji_usage: "多久用一次表情",
  verbosity: "一次回复说多少",
  conversation_initiative: "会不会主动起话头",
  self_disclosure: "愿不愿意讲自己的事",
  social_distance: "说话的距离感",
  disagreement_style: "不认同对方时的处理方式",
  topic_transition: "从一个话题跳到另一个的方式",
};

/** 取值中文名。按「字段 → 取值」两级，避免不同字段同名取值互相污染。 */
const VALUE_LABEL: Record<string, Record<string, string>> = {
  sentence_rhythm: { clipped: "短促", measured: "平稳", flowing: "流畅", fragmented: "碎片" },
  directness: { blunt: "率直", direct: "直接", diplomatic: "委婉", indirect: "迂回" },
  humor_style: { none: "不幽默", dry: "冷幽默", sarcastic: "讽刺", playful: "俏皮", absurd: "荒诞" },
  emotional_expression: { reserved: "内敛", warm: "温和", expressive: "外放", volatile: "易波动" },
  vocabulary_style: { plain: "平实", precise: "精确", literary: "文艺", street: "街头", technical: "技术" },
  slang_usage: { none: "不用", rare: "很少", moderate: "偶尔", heavy: "频繁" },
  emoji_usage: { never: "从不", rare: "很少", sometimes: "有时", often: "经常" },
  verbosity: { one_line: "一句话", short: "简短", medium: "中等", long: "长篇" },
  conversation_initiative: { low: "被动", medium: "一般", high: "主动" },
  self_disclosure: { guarded: "戒备", measured: "适度", open: "坦率" },
  social_distance: { distant: "疏远", neutral: "中性", familiar: "熟络", intimate: "亲密" },
  disagreement_style: {
    avoid: "回避",
    soften: "缓和",
    state_plainly: "直说",
    challenge: "质疑",
  },
  topic_transition: {
    stay: "不换",
    gentle_shift: "自然过渡",
    abrupt_shift: "突然跳",
    callback: "绕回旧话",
  },
};

function fieldLabel(key: string): string {
  return FIELD_LABEL[key] ?? key;
}

function valueLabel(field: string, value: string): string {
  return VALUE_LABEL[field]?.[value] ?? value;
}

/* -------------------------------------------------------------------------- */
/* 编辑器                                                                      */
/* -------------------------------------------------------------------------- */

export interface SpeechDnaValue {
  [key: string]: unknown;
}

export function SpeechDnaEditor({
  value,
  onChange,
  disabled,
}: {
  value: SpeechDnaValue;
  onChange: (next: SpeechDnaValue) => void;
  disabled?: boolean;
}) {
  const lexicon = useQuery({
    queryKey: adminKeys.lexicon("dna"),
    queryFn: () => apiGet<DnaLexicon>("/api/admin/lexicons/dna"),
    // 词库改动不频繁；和 DnaSection 共用同一个缓存 key，改完那边自动刷新
    staleTime: 60_000,
  });

  const fields = lexicon.data?.data?.fields ?? {};

  /** 按词库顺序排好的枚举字段（只取有 values 的；停用的取值不展示）。 */
  const enumFields = useMemo(() => {
    return Object.entries(fields)
      .map(([key, field]) => {
        const values = Object.entries(field?.values ?? {})
          .filter(([, meta]) => meta?.enabled !== false)
          .map(([name, meta]) => ({
            name,
            label: valueLabel(key, name),
            trait: typeof meta?.trait === "number" ? meta.trait : null,
          }))
          // 按 trait 升序 = 词库作者的语义顺序（短促→碎片）；没有 trait 的排后面
          .sort((a, b) => (a.trait ?? 99) - (b.trait ?? 99));
        return { key, values, default: field?.default ?? "" };
      })
      .filter((field) => field.values.length > 0);
  }, [fields]);

  /** 当前值不在词库可选范围内（老数据/手写值）时，要露出来而不是悄悄丢掉。 */
  const unknownValues = useMemo(() => {
    const out: Array<{ field: string; value: string }> = [];
    for (const field of enumFields) {
      const current = String((value[field.key] as string) ?? "");
      if (current && !field.values.some((item) => item.name === current)) {
        out.push({ field: field.key, value: current });
      }
    }
    return out;
  }, [enumFields, value]);

  const setEnum = (key: string, next: string) => {
    const copy = { ...value };
    if (next) copy[key] = next;
    else delete copy[key];
    onChange(copy);
  };

  const listValue = (key: string): string[] => {
    const raw = value[key];
    return Array.isArray(raw) ? raw.map((item) => String(item)) : [];
  };

  const setList = (key: string, next: string[]) => {
    const copy = { ...value };
    if (next.length) copy[key] = next;
    else delete copy[key];
    onChange(copy);
  };

  const textValue = (key: string): string => {
    const raw = value[key];
    return typeof raw === "string" ? raw : "";
  };

  const setText = (key: string, next: string) => {
    const copy = { ...value };
    if (next) copy[key] = next;
    else delete copy[key];
    onChange(copy);
  };

  if (lexicon.isLoading) {
    return (
      <div className="admin-card" style={{ padding: 12 }}>
        <span className="admin-muted">正在读取表达 DNA 词库…</span>
      </div>
    );
  }

  const failed = lexicon.data?.ok === false || Boolean(lexicon.error);

  return (
    <div className="admin-col" style={{ gap: 14 }}>
      <div className="admin-row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
        <p className="admin-muted" style={{ fontSize: 12, margin: 0 }}>
          这些是让角色"说话像个人"的旋钮。留空 = 用词库默认值。
        </p>
        {failed ? (
          <Badge tone="warn">词库读取失败，只能编辑已有值</Badge>
        ) : (
          <Badge tone="success">{enumFields.length} 个维度</Badge>
        )}
      </div>

      {enumFields.length === 0 ? (
        <div className="admin-card">
          <p className="admin-muted" style={{ margin: 0, fontSize: 13 }}>
            {failed
              ? "词库 /api/admin/lexicons/dna 读取失败。先在「表达 DNA」页确认词库可用，否则这里没有可选值（已有值不会被清除）。"
              : "词库里没有可用的枚举字段。"}
          </p>
        </div>
      ) : (
        enumFields.map((field) => {
          const current = String((value[field.key] as string) ?? "");
          const hasTrait = field.values.some((item) => item.trait !== null);
          // 滑杆位置取当前值的 trait；没有当前值就用默认值的位置
          const currentTrait =
            field.values.find((item) => item.name === current)?.trait ??
            field.values.find((item) => item.name === field.default)?.trait ??
            null;
          const traitList = field.values
            .map((item) => item.trait)
            .filter((item): item is number => item !== null);
          const minTrait = traitList.length ? Math.min(...traitList) : 0;
          const maxTrait = traitList.length ? Math.max(...traitList) : 1;
          const sliderValue = currentTrait ?? (minTrait + maxTrait) / 2;

          return (
            <div className="admin-card" key={field.key} style={{ padding: "12px 14px" }}>
              <div
                className="admin-row"
                style={{ justifyContent: "space-between", alignItems: "baseline", marginBottom: 6 }}
              >
                <strong style={{ fontSize: 13 }}>{fieldLabel(field.key)}</strong>
                <span className="admin-muted admin-mono" style={{ fontSize: 11 }}>
                  {field.key}
                </span>
              </div>
              {FIELD_HINT[field.key] ? (
                <p className="admin-muted" style={{ fontSize: 11.5, margin: "0 0 8px" }}>
                  {FIELD_HINT[field.key]}
                </p>
              ) : null}

              {/* 分段按钮：精确点选。
                  「默认」这一项很重要：角色没存这个字段和显式存成默认值，
                  在这个编辑器里必须都能回到/保持"未设置"，否则运营点一下就被钉死。 */}
              <div className="admin-row" style={{ flexWrap: "wrap", gap: 6, marginBottom: 8 }}>
                <button
                  type="button"
                  className="admin-btn"
                  disabled={disabled}
                  onClick={() => setEnum(field.key, "")}
                  style={
                    current === ""
                      ? { background: "var(--md-sys-color-primary)", color: "var(--md-sys-color-on-primary)" }
                      : { opacity: 0.75 }
                  }
                  title="清空该字段，使用词库默认值"
                >
                  默认{field.default ? `（${valueLabel(field.key, field.default)}）` : ""}
                </button>
                {field.values.map((item) => (
                  <button
                    key={item.name}
                    type="button"
                    className="admin-btn"
                    disabled={disabled}
                    onClick={() => setEnum(field.key, item.name)}
                    style={
                      current === item.name
                        ? { background: "var(--md-sys-color-primary)", color: "var(--md-sys-color-on-primary)" }
                        : undefined
                    }
                    title={item.trait === null ? item.name : `${item.name} · trait ${item.trait}`}
                  >
                    {item.label}
                  </button>
                ))}
                {current && !field.values.some((item) => item.name === current) ? (
                  <Badge tone="warn">{current}（词库外）</Badge>
                ) : null}
              </div>

              {/* 滑杆：表达"往哪边调"，吸附到最近的可选值 */}
              {hasTrait ? (
                <Slider
                  label="强度"
                  min={minTrait}
                  max={maxTrait}
                  step={0.01}
                  value={sliderValue}
                  disabled={disabled}
                  // 文案要诚实：没存值时滑块位置只是"默认值落在哪"，不能说"当前值"
                  hint={
                    current
                      ? `${valueLabel(field.key, current)} · ${sliderValue.toFixed(2)}`
                      : `未设置，按词库默认「${valueLabel(field.key, field.default)}」· ${sliderValue.toFixed(2)}`
                  }
                  onChange={(next) => {
                    // 滑杆不产生新取值：吸附到 trait 最近的那个合法值
                    let best = field.values[0];
                    let bestGap = Infinity;
                    for (const item of field.values) {
                      if (item.trait === null) continue;
                      const gap = Math.abs(item.trait - next);
                      if (gap < bestGap) {
                        bestGap = gap;
                        best = item;
                      }
                    }
                    if (best && best.name !== current) setEnum(field.key, best.name);
                  }}
                />
              ) : null}
            </div>
          );
        })
      )}

      {/* 两个短语列表 ---------------------------------------------------- */}
      <div className="admin-card" style={{ padding: "12px 14px" }}>
        <Field
          label="口头禅 / 招牌说法"
          hint="她常挂在嘴边的短语，回车添加。留空 = 不限制"
        >
          <TagCloud
            tags={listValue("signature_phrases")}
            onAdd={
              disabled
                ? undefined
                : (tag) => setList("signature_phrases", [...listValue("signature_phrases"), tag])
            }
            onRemove={
              disabled
                ? undefined
                : (tag) =>
                    setList(
                      "signature_phrases",
                      listValue("signature_phrases").filter((item) => item !== tag),
                    )
            }
            placeholder="输入后回车，例如 honestly"
          />
        </Field>
      </div>

      <div className="admin-card" style={{ padding: "12px 14px" }}>
        <Field label="禁用词" hint="这个角色绝不会说的话，回车添加">
          <TagCloud
            tags={listValue("banned_phrases")}
            onAdd={
              disabled
                ? undefined
                : (tag) => setList("banned_phrases", [...listValue("banned_phrases"), tag])
            }
            onRemove={
              disabled
                ? undefined
                : (tag) =>
                    setList(
                      "banned_phrases",
                      listValue("banned_phrases").filter((item) => item !== tag),
                    )
            }
            placeholder="输入后回车"
          />
        </Field>
      </div>

      {/* 两个自由文本 ---------------------------------------------------- */}
      <div className="admin-row" style={{ gap: 12, alignItems: "flex-start", flexWrap: "wrap" }}>
        <div style={{ flex: "1 1 240px" }}>
          <Field label="标点习惯" hint="可留空，例如「从不用感叹号」">
            <TextInput
              value={textValue("punctuation_habit")}
              disabled={disabled}
              onChange={(next) => setText("punctuation_habit", next)}
            />
          </Field>
        </div>
        <div style={{ flex: "1 1 240px" }}>
          <Field label="备注" hint="给运营自己看的说明，不进 prompt">
            <TextInput
              value={textValue("note")}
              disabled={disabled}
              onChange={(next) => setText("note", next)}
            />
          </Field>
        </div>
      </div>

      {/* 词库外的值：显式提示，避免"看着没了其实还在" -------------------- */}
      {unknownValues.length > 0 ? (
        <div className="admin-card" style={{ padding: "10px 14px" }}>
          <p className="admin-muted" style={{ margin: 0, fontSize: 12 }}>
            有 {unknownValues.length} 个值不在当前词库里（不会丢失，保存时原样带回）：
            {unknownValues.map((item) => (
              <span key={`${item.field}:${item.value}`} className="admin-mono" style={{ marginLeft: 6 }}>
                {item.field}={item.value}
              </span>
            ))}
          </p>
        </div>
      ) : null}
    </div>
  );
}

/** 未知 key 的兜底展示（词库里没有、角色数据里却有）。 */
export function SpeechDnaExtras({
  value,
  knownKeys,
}: {
  value: SpeechDnaValue;
  knownKeys: string[];
}) {
  const extras = Object.keys(value).filter((key) => !knownKeys.includes(key));
  if (extras.length === 0) return null;
  return (
    <p className="admin-muted admin-mono" style={{ fontSize: 11, margin: 0 }}>
      额外字段：{extras.join(", ")}
    </p>
  );
}
