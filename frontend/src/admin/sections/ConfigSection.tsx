"use client";

/**
 * API 与密钥：LLM / STT / TTS / Deepgram 流式识别 四张配置卡。
 *
 * 设计要点
 * --------
 * 1. **密钥只显示指纹**：后端 `snapshot()` 只返回 `masked_key`，前端永远拿不到明文，
 *    也不该拿——这是"改不坏线上"的一部分。
 * 2. **保存即验证**：后端默认"先连通测试、通过才落盘"，验证失败不保存。
 *    界面上把「保存」与「测试连接」分开，让运营能先测再存。
 * 3. **来源可见**：每张卡标出当前值来自「环境变量」还是「后台」，
 *    否则运维改了半天发现是被 `.env` 覆盖，会非常困惑。
 *
 * 关于命名：这里**没有**"流式 TTS"。流式的是 Deepgram 的**语音识别**，
 * 合成（TTS）只有 Fish / Edge，按句合成。旧文档曾把它写成流式 TTS，已纠正。
 */

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { AdminApiError, apiGet, apiPost, apiPut } from "../api";
import { adminKeys } from "../queryKeys";
import { Badge, Button, ErrorState, Field, LoadingState, TextInput, useToasts, ToastStack } from "../ui";

/* -------------------------------------------------------------------------- */
/* 类型                                                                        */
/* -------------------------------------------------------------------------- */

interface ConfigSnapshot {
  llm?: Record<string, unknown> & { configured?: boolean; masked_key?: string; error?: string };
  stt?: Record<string, unknown> & { error?: string };
  tts?: Record<string, unknown> & { error?: string };
  fish?: Record<string, unknown>;
  deepgram?: Record<string, unknown> & { available?: boolean; error?: string };
  /** JPush：`configured` + `source`（env/admin/settings）+ `app_key_tail`（脱敏） */
  jpush?: Record<string, unknown> & {
    configured?: boolean;
    source?: string;
    app_key_tail?: string;
    has_master_secret?: boolean;
    error?: string;
  };
  client_locked?: boolean;
}

type Target = "llm" | "stt" | "tts" | "deepgram" | "jpush";

/** 每张卡要显示的字段（顺序即表单顺序）。值来自后端 `_LLM_FIELDS` 等常量。 */
const CARD_FIELDS: Record<Target, Array<{ key: string; label: string; secret?: boolean; hint?: string }>> = {
  llm: [
    { key: "base_url", label: "Base URL", hint: "OpenAI 兼容端点，例如 https://api.deepseek.com/v1；只填域名会自动补 /v1" },
    { key: "model", label: "模型", hint: "主对话模型" },
    { key: "api_key", label: "API Key", secret: true, hint: "只显示指纹；留空表示不修改" },
    { key: "suggestion_model", label: "建议模型", hint: "可留空，默认跟随主模型" },
    { key: "temperature", label: "temperature" },
    { key: "max_tokens", label: "max_tokens" },
  ],
  stt: [
    { key: "provider", label: "提供方", hint: "如 siliconflow / whisper" },
    { key: "base_url", label: "Base URL" },
    { key: "model", label: "模型" },
    { key: "api_key", label: "API Key", secret: true },
    { key: "language", label: "语种", hint: "如 en / zh；留空自动" },
    { key: "timeout_s", label: "超时（秒）" },
  ],
  tts: [
    { key: "provider", label: "提供方", hint: "fish / edge / tone" },
    { key: "fish_api_key", label: "Fish API Key", secret: true },
    { key: "fish_base_url", label: "Fish Base URL" },
    { key: "fish_model", label: "Fish 模型" },
    { key: "fish_reference_id", label: "默认音色 id" },
    { key: "fish_format", label: "音频格式" },
  ],
  deepgram: [
    { key: "api_key", label: "Deepgram API Key", secret: true, hint: "通话实时识别用；不是 TTS" },
    { key: "model", label: "模型" },
    { key: "language", label: "语种" },
  ],
  jpush: [
    { key: "app_key", label: "AppKey", hint: "极光推送控制台 → 应用设置" },
    { key: "master_secret", label: "MasterSecret", secret: true, hint: "留空表示不修改已保存的值" },
  ],
};

const CARD_TITLE: Record<Target, string> = {
  llm: "文本模型 LLM",
  stt: "语音识别 STT",
  tts: "语音合成 TTS",
  deepgram: "Deepgram 流式识别",
  jpush: "JPush 推送",
};

const CARD_DESC: Record<Target, string> = {
  llm: "对话、捏人、生活事件等所有文本生成。",
  stt: "语音条转写（非实时）。",
  tts: "把回复合成语音；Fish 需要有效 key，Edge 免费兜底。",
  deepgram: "通话中的实时语音识别（流式）。注意：这是识别，不是合成。",
  jpush: "好友申请、动态互动等的手机通知。改完立即生效，不需要重启。",
};

/* -------------------------------------------------------------------------- */
/* 单张配置卡                                                                  */
/* -------------------------------------------------------------------------- */

/** JPush 的凭据来源提示。
 *
 * 为什么必须显示：优先级是「环境变量 > 管理台保存 > .env」。
 * 如果运维在环境变量里设了 JPUSH_APP_KEY，运营在后台怎么改都不生效，
 * 而界面上又显示"已配置"——不说清楚就会变成"改了没用"的玄学问题。
 */
function JpushSourceNote({ data }: { data: Record<string, unknown> | undefined }) {
  const source = typeof data?.source === "string" ? data.source : "";
  const tail = typeof data?.app_key_tail === "string" ? data.app_key_tail : "";
  if (!source) {
    return (
      <p className="admin-muted" style={{ marginTop: 0 }}>
        还没配置。填好 AppKey 与 MasterSecret 后点保存，系统会先做一次鉴权再落盘。
      </p>
    );
  }
  const label =
    source === "env"
      ? "环境变量（会盖过这里的设置，改后台不生效）"
      : source === "admin"
        ? "管理台保存"
        : ".env 文件";
  return (
    <p className="admin-muted" style={{ marginTop: 0 }}>
      当前来源：<strong>{label}</strong>
      {tail ? <span className="admin-mono"> · AppKey …{tail}</span> : null}
      {source === "env" ? (
        <>
          <br />
          要让后台设置生效，先清掉服务器上的 <span className="admin-mono">JPUSH_APP_KEY</span> /{" "}
          <span className="admin-mono">JPUSH_MASTER_SECRET</span> 环境变量。
        </>
      ) : null}
    </p>
  );
}

function ConfigCard({
  target,
  data,
  onSaved,
}: {
  target: Target;
  data: Record<string, unknown> | undefined;
  onSaved: () => void;
}) {
  const fields = CARD_FIELDS[target];
  const { toasts, push } = useToasts();
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [issues, setIssues] = useState<string[]>([]);
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null);

  // 后端返回新值后，清掉草稿（草稿只表示"用户改过但没保存"）。
  useEffect(() => {
    setDraft({});
  }, [data]);

  const error = typeof data?.error === "string" ? data.error : "";
  const maskedKey = typeof data?.masked_key === "string" ? data.masked_key : "";
  const configured = data?.configured === true || Boolean(maskedKey);
  const available = data?.available;

  const valueOf = (key: string): string => {
    if (key in draft) return draft[key];
    const raw = data?.[key];
    return raw === undefined || raw === null ? "" : String(raw);
  };

  const save = async () => {
    // 只提交真正改过的字段——避免把没动的 secret 字段用空串覆盖掉。
    const payload: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(draft)) {
      if (value === "") continue;
      payload[key] = key === "temperature" || key === "max_tokens" || key === "timeout_s"
        ? Number(value)
        : value;
    }
    if (Object.keys(payload).length === 0) {
      push("没有要保存的改动", "error");
      return;
    }
    setSaving(true);
    setIssues([]);
    try {
      const result = await apiPut<{ ok?: boolean; issues?: string[]; message?: string }>(
        `/api/admin/config/${target}`,
        payload,
      );
      if (result.ok === false) {
        setIssues(result.issues ?? [result.message ?? "保存失败"]);
        push("保存未通过验证，未落盘", "error");
      } else {
        push(`${CARD_TITLE[target]} 已保存`);
        setDraft({});
        onSaved();
      }
    } catch (cause) {
      const err = cause as AdminApiError;
      setIssues(err.issues?.length ? err.issues : [err.detail || String(cause)]);
      push("保存失败", "error");
    } finally {
      setSaving(false);
    }
  };

  const test = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const result = await apiPost<{ ok?: boolean; message?: string }>(
        `/api/admin/config/${target}/test`,
      );
      setTestResult({
        ok: result.ok === true,
        message: result.message || (result.ok ? "连接正常" : "连接失败"),
      });
    } catch (cause) {
      setTestResult({ ok: false, message: cause instanceof AdminApiError ? cause.detail : String(cause) });
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="admin-card">
      <ToastStack toasts={toasts} />
      <div className="admin-card-head">
        <h2 className="admin-card-title">{CARD_TITLE[target]}</h2>
        {error ? (
          <Badge tone="error">读取失败</Badge>
        ) : target === "deepgram" ? (
          <Badge tone={available ? "success" : undefined}>{available ? "可用" : "未配置"}</Badge>
        ) : (
          <Badge tone={configured ? "success" : undefined}>{configured ? "已配置" : "未配置"}</Badge>
        )}
      </div>
      <p className="admin-card-sub" style={{ marginTop: 0 }}>{CARD_DESC[target]}</p>

      {error ? <ErrorState title="读取配置失败" message={error} /> : null}

      {target === "llm" && maskedKey ? (
        <p className="admin-mono admin-muted" style={{ marginTop: 0 }}>
          当前密钥：{maskedKey}
        </p>
      ) : null}

      {target === "jpush" ? <JpushSourceNote data={data} /> : null}

      <div className="admin-form-grid">
        {fields.map((field) => (
          <Field
            key={field.key}
            label={field.label}
            hint={field.hint}
            htmlFor={`${target}-${field.key}`}
          >
            <TextInput
              id={`${target}-${field.key}`}
              value={valueOf(field.key)}
              type={field.secret ? "password" : "text"}
              placeholder={field.secret ? "留空表示不修改" : ""}
              onChange={(value) => setDraft((current) => ({ ...current, [field.key]: value }))}
            />
          </Field>
        ))}
      </div>

      {issues.length ? (
        <div className="admin-error" style={{ marginTop: 12 }} role="alert">
          <strong>验证未通过，未保存</strong>
          <ul className="admin-error-issues">
            {issues.map((issue) => (
              <li key={issue}>{issue}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {testResult ? (
        <p style={{ marginBottom: 0 }}>
          <Badge tone={testResult.ok ? "success" : "error"}>
            {testResult.ok ? "连接正常" : "连接失败"}
          </Badge>{" "}
          <span className="admin-muted">{testResult.message}</span>
        </p>
      ) : null}

      <div className="admin-row" style={{ marginTop: 12 }}>
        <Button variant="primary" onClick={() => void save()} loading={saving}>
          保存（先验证再落盘）
        </Button>
        <Button onClick={() => void test()} loading={testing}>
          测试连接
        </Button>
        {Object.keys(draft).length > 0 ? (
          <Button variant="ghost" onClick={() => setDraft({})}>
            撤销改动
          </Button>
        ) : null}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 分区                                                                        */
/* -------------------------------------------------------------------------- */

export default function ConfigSection() {
  const query = useQuery({
    queryKey: adminKeys.config(),
    queryFn: () => apiGet<ConfigSnapshot>("/api/admin/config"),
  });

  if (query.isPending) return <LoadingState label="正在读取配置…" />;
  if (query.isError) {
    const error = query.error;
    return (
      <ErrorState
        title="读取配置失败"
        message={error instanceof AdminApiError ? error.detail : String(error)}
        onRetry={() => void query.refetch()}
      />
    );
  }

  const data = query.data;

  return (
    <section className="admin-section">
      <div className="admin-section-head">
        <div>
          <h1 className="admin-section-title">API 与密钥</h1>
          <p className="admin-section-desc">
            密钥只以指纹形式显示，改动需要先通过连通测试才会落盘。保存后立即生效，不需要重启。
          </p>
        </div>
        <div className="admin-section-actions">
          <Button onClick={() => void query.refetch()} disabled={query.isFetching}>
            {query.isFetching ? "刷新中…" : "刷新"}
          </Button>
        </div>
      </div>

      <div className="admin-col" style={{ gap: 20 }}>
        {(["llm", "stt", "tts", "deepgram", "jpush"] as Target[]).map((target) => (
          <ConfigCard
            key={target}
            target={target}
            data={data[target] as Record<string, unknown> | undefined}
            onSaved={() => void query.refetch()}
          />
        ))}
      </div>

      <p className="admin-muted" style={{ fontSize: 12 }}>
        说明：Deepgram 用于通话中的<strong>实时语音识别</strong>（流式）；语音合成（TTS）仍是
        Fish / Edge 按句合成，没有流式 TTS。
      </p>
    </section>
  );
}
