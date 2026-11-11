"use client";

/**
 * 用户详情（`#/users/{uid}`）：概览 + 设备与推送 + 好友 + 指派好友。
 *
 * 用的接口
 * --------
 * - `GET    /api/admin/users/{uid}`
 *     → `{ok, user, profile, devices:[{registration_id, platform, vendor, enabled,
 *         updated_at}], friends:[{id, persona_id, persona_nickname, persona_avatar,
 *         status, created_at}], posts:[...], counts}`
 * - `GET    /api/admin/users/{uid}/candidate-personas?q=&limit=20`
 *     → `{ok, items:[{id, nickname, avatar, occupation, city, language, is_friend,
 *         match_hint}]}`
 * - `POST   /api/admin/users/{uid}/friends`  body `{persona_id, mode:"request"|"direct", message?, notify?}`
 * - `DELETE /api/admin/users/{uid}/friends/{friendship_id}`
 * - `POST   /api/admin/users/{uid}/push-test`（JPush 未配置时后端用 `issues[]` 说明，原样展示）
 *
 * 产品决定（不是漏做）
 * --------------------
 * 1. **管理台不提供聊天记录查看**。本页只读账号、设备、好友、计数这些元数据，
 *    不读用户与角色的对话内容——页面上有一句明确说明，而不是留白。
 * 2. **好友指派有两种模式，区别必须在界面上讲清楚**：
 *    - 「发好友申请」= 给用户推通知，**要用户同意**后才出现在好友列表（默认）；
 *    - 「直接成为好友」= **跳过同意**，用户好友列表里立刻出现。
 *    这不是可选细节，所以两种模式各占一块说明 + 每行两个独立按钮。
 *
 * 为什么 `userId` 是可选 prop 而不是必填
 * --------------------------------------
 * `router.ts` 解析出的是 `{name:"userDetail", userId}`，但 `AdminApp.tsx` 里
 * `<UserDetailSection />` 是否传 prop 由整合方决定（本任务不允许改 AdminApp）。
 * 所以这里两种接线都支持：给了 prop 就用 prop，没给就从 `location.hash` 兜底解析，
 * 并订阅 `hashchange`，在详情之间切换时自动跟随。
 */

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { AdminApiError, apiDelete, apiGet, apiPost, withQuery } from "../api";
import { adminKeys } from "../queryKeys";
import { adminRouteToHash, parseAdminRoute } from "../router";
import {
  Badge,
  Button,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Field,
  LoadingState,
  Table,
  TextInput,
  ToastStack,
  useToasts,
} from "../ui";

/* -------------------------------------------------------------------------- */
/* 后端数据形状                                                                */
/* -------------------------------------------------------------------------- */

type ApiRecord = Record<string, unknown>;
type Tone = "success" | "error" | "warn" | "info" | undefined;

interface UserCounts {
  posts?: number;
  friends?: number;
  devices?: number;
}

interface UserRow {
  id?: string;
  display_name?: string;
  device_key_tail?: string;
  device_key_masked?: string;
  created_at?: string;
  last_seen_at?: string;
  [key: string]: unknown;
}

interface DeviceRow {
  registration_id?: string;
  platform?: string;
  vendor?: string;
  enabled?: unknown;
  updated_at?: string;
}

interface FriendRow {
  /** 好友关系 id（DELETE 用的 `friendship_id`）。 */
  id: string;
  persona_id?: string;
  persona_nickname?: string;
  persona_avatar?: string;
  status?: string;
  created_at?: string;
  request_message?: string;
}

interface CandidateRow {
  id: string;
  nickname?: string;
  avatar?: string;
  occupation?: string;
  city?: string;
  language?: string;
  is_friend?: boolean;
  match_hint?: string;
}

interface UserDetailPayload {
  ok?: boolean;
  issues?: string[];
  user?: UserRow;
  profile?: unknown;
  devices?: DeviceRow[];
  friends?: FriendRow[];
  posts?: unknown[];
  counts?: UserCounts;
}

interface CandidatePayload {
  ok?: boolean;
  issues?: string[];
  items?: CandidateRow[];
}

interface PushResult {
  ok: boolean;
  issues: string[];
  raw: ApiRecord;
}

type AssignMode = "request" | "direct";

const CANDIDATE_LIMIT = 20;

/* -------------------------------------------------------------------------- */
/* 小工具                                                                      */
/* -------------------------------------------------------------------------- */

function asText(value: unknown, fallback = ""): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
}

function asNumber(value: unknown): number {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : 0;
}

function issueText(issues: unknown): string {
  const list = Array.isArray(issues) ? issues.map((item) => String(item)) : [];
  return list.length ? list.join("；") : "操作失败（后端未返回原因）";
}

function errorMessage(error: unknown): string {
  if (error instanceof AdminApiError) {
    return error.issues.join("；") || error.detail || error.message;
  }
  if (error instanceof Error) return error.message;
  return String(error);
}

/** 业务失败（HTTP 200 + `ok:false`）也当异常抛，统一走 onError / toast。 */
function ensureOk<T extends ApiRecord>(result: T): T {
  if (result.ok === false) throw new Error(issueText(result.issues));
  return result;
}

/** 秒级 epoch / 毫秒 epoch / ISO 文本 三种时间都吃。 */
function formatTime(value: unknown): string {
  if (value === null || value === undefined) return "";
  let date: Date | null = null;
  if (typeof value === "number") {
    date = new Date(value < 1e11 ? value * 1000 : value);
  } else if (typeof value === "string") {
    const raw = value.trim();
    if (!raw) return "";
    if (/^\d+$/.test(raw)) {
      const numeric = Number(raw);
      date = new Date(numeric < 1e11 ? numeric * 1000 : numeric);
    } else {
      const parsed = Date.parse(raw.includes("T") ? raw : raw.replace(" ", "T"));
      date = Number.isNaN(parsed) ? null : new Date(parsed);
    }
  }
  if (!date || Number.isNaN(date.getTime())) return typeof value === "string" ? value : "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`;
}

/** 展示用表格单元格文本：对象/数组压成一行，超长截断（完整值放 title）。 */
function cellText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return value.map((item) => cellText(item)).filter(Boolean).join("、");
  try {
    const text = JSON.stringify(value);
    return text.length > 180 ? `${text.slice(0, 180)}…` : text;
  } catch {
    return "";
  }
}

function tailOf(value: unknown, size = 8): string {
  const text = asText(value);
  if (!text) return "";
  return text.length <= size ? text : `…${text.slice(-size)}`;
}

function isEnabledFlag(value: unknown): boolean {
  return value === true || value === 1 || value === "1" || value === "true";
}

function friendStatusMeta(status: unknown): { label: string; tone: Tone } {
  const raw = asText(status).trim();
  switch (raw) {
    case "accepted":
      return { label: "已是好友", tone: "success" };
    case "pending":
    case "requested":
      return { label: "待用户同意", tone: "warn" };
    case "rejected":
      return { label: "用户拒绝", tone: "error" };
    case "blocked":
      return { label: "已屏蔽", tone: "error" };
    case "cancelled":
    case "canceled":
      return { label: "已取消", tone: undefined };
    default:
      return { label: raw || "未知", tone: undefined };
  }
}

const PROFILE_LABEL: Record<string, string> = {
  nickname: "昵称",
  native_language: "母语",
  target_languages: "学习语言",
  birthday: "生日",
  fact_sheet: "事实表（fact_sheet）",
  updated_at: "资料更新时间",
  profile_setup_completed_at: "资料填写完成时间",
};

/** `profile` 里已经在别处展示、不需要重复的字段。 */
const PROFILE_SKIP = new Set(["id", "user_id"]);

/** 候选人查询 key：`queryKeys.ts` 没有这个 key，且本任务不允许改它。 */
function candidateKey(uid: string, q: string) {
  return ["admin", "user", uid, "candidates", q] as const;
}

function goBackToList(): void {
  window.location.hash = adminRouteToHash({ name: "users" });
}

/* -------------------------------------------------------------------------- */
/* 局部展示组件                                                                */
/* -------------------------------------------------------------------------- */

function Stat({ label, value, hint }: { label: string; value: number | string; hint?: string }) {
  return (
    <div className="admin-stat">
      <div className="admin-stat-label">{label}</div>
      <div className="admin-stat-value">{value}</div>
      {hint ? <div className="admin-stat-hint">{hint}</div> : null}
    </div>
  );
}

interface KV {
  key: string;
  label: string;
  value: string;
}

function KeyValueTable({ rows }: { rows: KV[] }) {
  if (!rows.length) return null;
  return (
    <Table<KV>
      rows={rows}
      rowKey={(row) => row.key}
      columns={[
        { key: "label", header: "字段", width: "200px", render: (row) => row.label },
        {
          key: "value",
          header: "值",
          render: (row) => (
            <span className="admin-mono" title={row.value} style={{ wordBreak: "break-all" }}>
              {row.value}
            </span>
          ),
        },
      ]}
    />
  );
}

function PersonaAvatar({ url, name }: { url?: string; name?: string }) {
  const src = asText(url);
  const label = asText(name) || "?";
  if (!src) {
    return <span className="admin-mono admin-muted">{label.slice(0, 2)}</span>;
  }
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={src}
      alt={label}
      loading="lazy"
      style={{ width: 28, height: 28, borderRadius: 999, objectFit: "cover", display: "block" }}
    />
  );
}

/* -------------------------------------------------------------------------- */
/* 分区                                                                        */
/* -------------------------------------------------------------------------- */

export default function UserDetailSection({ userId }: { userId?: string } = {}) {
  const queryClient = useQueryClient();
  const { toasts, push } = useToasts();

  const [hashUserId, setHashUserId] = useState("");

  useEffect(() => {
    const read = () => {
      const route = parseAdminRoute(window.location.hash);
      setHashUserId(route.name === "userDetail" ? route.userId : "");
    };
    read();
    window.addEventListener("hashchange", read);
    return () => window.removeEventListener("hashchange", read);
  }, []);

  const uid = asText(userId).trim() || hashUserId;

  const [candidateInput, setCandidateInput] = useState("");
  const [candidateQ, setCandidateQ] = useState("");
  const [requestMessage, setRequestMessage] = useState("");
  const [notifyOnRequest, setNotifyOnRequest] = useState(true);
  const [removeTarget, setRemoveTarget] = useState<FriendRow | null>(null);
  const [pushResult, setPushResult] = useState<PushResult | null>(null);

  /* 换用户时清掉所有"跟上一个用户绑定"的临时状态。 */
  useEffect(() => {
    setCandidateInput("");
    setCandidateQ("");
    setRequestMessage("");
    setNotifyOnRequest(true);
    setRemoveTarget(null);
    setPushResult(null);
  }, [uid]);

  /* ------------------------------ 查询 ------------------------------ */

  const detailQuery = useQuery({
    queryKey: adminKeys.user(uid),
    queryFn: () => apiGet<UserDetailPayload>(`/api/admin/users/${encodeURIComponent(uid)}`),
    enabled: Boolean(uid),
  });

  const candidatesQuery = useQuery({
    queryKey: candidateKey(uid, candidateQ),
    queryFn: () =>
      apiGet<CandidatePayload>(
        withQuery(`/api/admin/users/${encodeURIComponent(uid)}/candidate-personas`, {
          q: candidateQ,
          limit: CANDIDATE_LIMIT,
        }),
      ),
    enabled: Boolean(uid),
  });

  /* ------------------------------ 写操作 ------------------------------ */

  const assignMutation = useMutation({
    mutationFn: async (request: { personaId: string; mode: AssignMode }) => {
      const body: ApiRecord = { persona_id: request.personaId, mode: request.mode };
      const message = requestMessage.trim();
      if (request.mode === "request") {
        if (message) body.message = message;
        body.notify = notifyOnRequest;
      }
      return ensureOk(
        await apiPost<ApiRecord>(`/api/admin/users/${encodeURIComponent(uid)}/friends`, body),
      );
    },
    onSuccess: (result, request) => {
      void queryClient.invalidateQueries({ queryKey: adminKeys.user(uid) });
      void queryClient.invalidateQueries({ queryKey: candidateKey(uid, candidateQ) });
      if (request.mode === "request") {
        const notified = result.notified ?? result.pushed ?? result.push;
        const suffix = notified === false ? "（通知未发出，见后端 issues）" : "";
        push(`已发送好友申请：用户同意后才会出现在 TA 的好友列表${suffix}`);
      } else {
        push("已直接成为好友：该角色现在就在用户的好友列表里（未经过用户同意）");
      }
    },
    onError: (error) => push(errorMessage(error), "error"),
  });

  const removeMutation = useMutation({
    mutationFn: async (friendshipId: string) =>
      ensureOk(
        await apiDelete<ApiRecord>(
          `/api/admin/users/${encodeURIComponent(uid)}/friends/${encodeURIComponent(friendshipId)}`,
        ),
      ),
    onSuccess: () => {
      setRemoveTarget(null);
      void queryClient.invalidateQueries({ queryKey: adminKeys.user(uid) });
      void queryClient.invalidateQueries({ queryKey: candidateKey(uid, candidateQ) });
      push("已解除好友关系（角色本身不受影响）");
    },
    onError: (error) => {
      setRemoveTarget(null);
      push(errorMessage(error), "error");
    },
  });

  const pushTestMutation = useMutation({
    mutationFn: async () =>
      apiPost<ApiRecord>(`/api/admin/users/${encodeURIComponent(uid)}/push-test`),
    onSuccess: (result) => {
      const issues = Array.isArray(result.issues) ? result.issues.map((item) => String(item)) : [];
      setPushResult({ ok: result.ok !== false && !issues.length, issues, raw: result });
      if (result.ok === false || issues.length) {
        push(issues.join("；") || "测试推送失败", "error");
      } else {
        push("测试推送已发出");
      }
    },
    onError: (error) => {
      const message = errorMessage(error);
      setPushResult({ ok: false, issues: [message], raw: {} });
      push(message, "error");
    },
  });

  /* ------------------------------ 派生数据 ------------------------------ */

  const data = detailQuery.data;
  const user: UserRow = data?.user ?? {};
  const counts: UserCounts = data?.counts ?? {};
  const devices = data?.devices ?? [];
  const friends = data?.friends ?? [];
  const candidates = candidatesQuery.data?.items ?? [];

  /** `profile` 的形状由后端决定（目前是 `user_profiles` 的一行），这里只做泛化展示。 */
  const profileRecord: ApiRecord | null =
    data?.profile && typeof data.profile === "object" && !Array.isArray(data.profile)
      ? (data.profile as ApiRecord)
      : null;

  const displayName =
    asText(user.display_name) || asText(profileRecord?.nickname) || "（未命名）";
  const deviceTail = asText(user.device_key_tail) || asText(user.device_key_masked);
  const friendTotal = asNumber(counts.friends ?? friends.length);
  const deviceTotal = asNumber(counts.devices ?? devices.length);

  const accountRows: KV[] = [
    { key: "id", label: "用户 id", value: uid },
    { key: "name", label: "昵称", value: displayName },
    { key: "device", label: "设备尾号", value: deviceTail },
    { key: "created", label: "创建时间", value: formatTime(user.created_at) },
    { key: "seen", label: "最近活跃", value: formatTime(user.last_seen_at) },
  ].filter((row) => row.value !== "");

  const profileRows: KV[] = [];
  if (profileRecord) {
    for (const [key, value] of Object.entries(profileRecord)) {
      if (PROFILE_SKIP.has(key)) continue;
      const text = cellText(value);
      if (!text) continue;
      profileRows.push({
        key,
        label: PROFILE_LABEL[key] || key,
        value: key.endsWith("_at") ? formatTime(value) || text : text,
      });
    }
  }

  /* ------------------------------ 交互 ------------------------------ */

  const applyCandidateSearch = () => setCandidateQ(candidateInput.trim());

  const clearCandidateSearch = () => {
    setCandidateInput("");
    setCandidateQ("");
  };

  const assign = (row: CandidateRow, mode: AssignMode) => {
    if (row.is_friend) {
      push("这个角色已经是 TA 的好友了", "error");
      return;
    }
    assignMutation.mutate({ personaId: row.id, mode });
  };

  const assignPending = (personaId: string, mode: AssignMode): boolean =>
    assignMutation.isPending &&
    assignMutation.variables?.personaId === personaId &&
    assignMutation.variables?.mode === mode;

  /* ------------------------------ 渲染 ------------------------------ */

  if (!uid) {
    return (
      <section className="admin-section">
        <EmptyState
          title="未指定用户"
          description="从「用户列表」点一行进来，或检查地址栏里的 #/users/{uid}。"
          action={<Button onClick={goBackToList}>返回用户列表</Button>}
        />
      </section>
    );
  }

  return (
    <section className="admin-section">
      <ToastStack toasts={toasts} />

      <div className="admin-section-head">
        <div>
          <h1 className="admin-section-title">用户详情</h1>
          <p className="admin-section-desc">
            {displayName} · <span className="admin-mono">{uid}</span>
            <br />
            这里只展示账号、设备、好友与计数的元数据。
            <strong>管理台不提供聊天记录查看</strong>
            ——用户与角色的对话内容不在运营侧读取，本页也不会出现任何对话文本。
          </p>
        </div>
        <div className="admin-section-actions">
          <Button variant="ghost" onClick={goBackToList}>
            返回列表
          </Button>
          <Button onClick={() => void detailQuery.refetch()} disabled={detailQuery.isFetching}>
            {detailQuery.isFetching ? "刷新中…" : "刷新"}
          </Button>
        </div>
      </div>

      {detailQuery.isError ? (
        <ErrorState
          title="读取用户详情失败"
          message={errorMessage(detailQuery.error)}
          issues={detailQuery.error instanceof AdminApiError ? detailQuery.error.issues : undefined}
          onRetry={() => void detailQuery.refetch()}
        />
      ) : detailQuery.isPending ? (
        <LoadingState label="正在读取用户详情…" />
      ) : data && data.ok === false ? (
        <ErrorState title="用户详情不可用" issues={data.issues} />
      ) : (
        <>
          {/* ---------- a. 概览 ---------- */}
          <div className="admin-card">
            <div className="admin-card-head">
              <h2 className="admin-card-title">概览</h2>
              <span className="admin-card-sub">{displayName}</span>
            </div>
            <div className="admin-stat-grid">
              <Stat label="动态" value={asNumber(counts.posts)} hint="该用户的动态数" />
              <Stat label="好友" value={friendTotal} hint="含待同意的申请" />
              <Stat label="推送设备" value={deviceTotal} hint="已注册的 registration_id" />
            </div>
            <KeyValueTable rows={accountRows} />
          </div>

          {/* ---------- 资料 profile ---------- */}
          {profileRows.length ? (
            <div className="admin-card">
              <div className="admin-card-head">
                <h2 className="admin-card-title">资料</h2>
                <span className="admin-card-sub">
                  {asText(profileRecord?.nickname) || "user_profiles"}
                </span>
              </div>
              <KeyValueTable rows={profileRows} />
            </div>
          ) : null}

          {/* ---------- b. 设备与推送 ---------- */}
          <div className="admin-card">
            <div className="admin-card-head">
              <h2 className="admin-card-title">设备与推送</h2>
              <span className="admin-card-sub">
                共 {devices.length} 台 · 只有启用的设备会收到推送
              </span>
              <div style={{ marginLeft: "auto" }}>
                <Button
                  size="sm"
                  loading={pushTestMutation.isPending}
                  onClick={() => pushTestMutation.mutate()}
                  title="向该用户已启用的设备发一条测试推送"
                >
                  发测试推送
                </Button>
              </div>
            </div>

            <Table<DeviceRow>
              rows={devices}
              rowKey={(row) =>
                [row.registration_id, row.platform, row.vendor, row.updated_at]
                  .map((item) => asText(item))
                  .filter(Boolean)
                  .join("|") || "device"
              }
              empty={
                <EmptyState
                  title="该用户还没有注册推送设备"
                  description="用户在 App 里开启通知后，这里会出现设备记录。未注册设备时「发测试推送」没有投递对象。"
                />
              }
              columns={[
                {
                  key: "id",
                  header: "设备 id 尾号",
                  render: (row) => (
                    <span className="admin-mono" title={asText(row.registration_id)}>
                      {tailOf(row.registration_id, 10)}
                    </span>
                  ),
                },
                { key: "platform", header: "平台", render: (row) => asText(row.platform) || null },
                { key: "vendor", header: "厂商", render: (row) => asText(row.vendor) || null },
                {
                  key: "enabled",
                  header: "状态",
                  render: (row) =>
                    isEnabledFlag(row.enabled) ? (
                      <Badge tone="success">启用</Badge>
                    ) : (
                      <Badge tone="warn">停用</Badge>
                    ),
                },
                {
                  key: "updated_at",
                  header: "更新时间",
                  render: (row) => formatTime(row.updated_at) || null,
                },
              ]}
            />

            {pushResult && pushResult.issues.length ? (
              <ErrorState
                title="测试推送未成功（以下是后端原样返回的原因）"
                issues={pushResult.issues}
              />
            ) : pushResult ? (
              <div className="admin-col">
                <p className="admin-muted" style={{ margin: 0 }}>
                  测试推送已发出。
                </p>
                <pre className="admin-mono" style={{ margin: 0, whiteSpace: "pre-wrap" }}>
                  {JSON.stringify(pushResult.raw, null, 2)}
                </pre>
              </div>
            ) : (
              <p className="admin-muted" style={{ margin: 0 }}>
                未配置 JPush 时后端会返回 `issues`，这里会原样列出原因，不会静默失败。
              </p>
            )}
          </div>

          {/* ---------- c. 好友列表 ---------- */}
          <div className="admin-card">
            <div className="admin-card-head">
              <h2 className="admin-card-title">好友列表</h2>
              <span className="admin-card-sub">共 {friends.length} 条关系</span>
            </div>

            <Table<FriendRow>
              rows={friends}
              rowKey={(row) => row.id}
              empty={
                <EmptyState
                  title="该用户还没有好友"
                  description="用下方的「指派好友」把角色加进来：默认发申请，勾选直连可跳过用户同意。"
                />
              }
              columns={[
                {
                  key: "persona",
                  header: "角色",
                  render: (row) => (
                    <div className="admin-row">
                      <PersonaAvatar url={row.persona_avatar} name={row.persona_nickname} />
                      <div className="admin-col" style={{ gap: 0 }}>
                        <span>{asText(row.persona_nickname) || "（角色已删除）"}</span>
                        <span className="admin-mono admin-muted">
                          {asText(row.persona_id) || row.id}
                        </span>
                      </div>
                    </div>
                  ),
                },
                {
                  key: "status",
                  header: "状态",
                  render: (row) => {
                    const meta = friendStatusMeta(row.status);
                    return <Badge tone={meta.tone}>{meta.label}</Badge>;
                  },
                },
                {
                  key: "created_at",
                  header: "建立时间",
                  render: (row) => formatTime(row.created_at) || null,
                },
                {
                  key: "actions",
                  header: "操作",
                  render: (row) => (
                    <Button
                      size="sm"
                      variant="danger"
                      loading={removeMutation.isPending && removeMutation.variables === row.id}
                      onClick={() => setRemoveTarget(row)}
                    >
                      解除
                    </Button>
                  ),
                },
              ]}
            />
          </div>

          {/* ---------- d. 指派好友 ---------- */}
          <div className="admin-card">
            <div className="admin-card-head">
              <h2 className="admin-card-title">指派好友</h2>
              <span className="admin-card-sub">搜索角色 → 选一种方式建立关系</span>
            </div>

            {/* 两种模式的区别：产品要求必须在界面上讲清楚 */}
            <div className="admin-grid-2">
              <div className="admin-col" style={{ gap: 6 }}>
                <div className="admin-row">
                  <Badge tone="info">发好友申请</Badge>
                  <strong>需要用户同意</strong>
                </div>
                <p className="admin-muted" style={{ margin: 0 }}>
                  给用户的 App 推一条好友申请通知；<strong>用户在 App 里点同意之后</strong>
                  ，这个角色才出现在 TA 的好友列表里。状态先是「待用户同意」。
                  这是默认方式，用户始终有选择权。
                </p>
              </div>
              <div className="admin-col" style={{ gap: 6 }}>
                <div className="admin-row">
                  <Badge tone="warn">直接成为好友</Badge>
                  <strong>跳过用户同意</strong>
                </div>
                <p className="admin-muted" style={{ margin: 0 }}>
                  <strong>不发申请、不等同意</strong>：角色立刻出现在用户的好友列表里，
                  用户不会收到「好友申请」这条通知。适合补数据 / 修线上问题，
                  但它是"替用户决定"，请确认确实需要再点。
                </p>
              </div>
            </div>

            <hr className="admin-divider" />

            <div className="admin-row">
              <div style={{ flex: "1 1 260px" }}>
                <TextInput
                  value={candidateInput}
                  onChange={setCandidateInput}
                  placeholder="搜索角色：昵称 / 职业 / 城市 / 语言"
                  type="search"
                  onEnter={applyCandidateSearch}
                />
              </div>
              <Button onClick={applyCandidateSearch}>搜索角色</Button>
              {candidateQ ? (
                <Button variant="ghost" onClick={clearCandidateSearch}>
                  清除
                </Button>
              ) : null}
            </div>

            <div className="admin-form-grid">
              <Field
                label="申请留言（可选）"
                htmlFor="assign-message"
                hint="仅「发好友申请」使用；「直接成为好友」不会带这条留言"
              >
                <TextInput
                  id="assign-message"
                  value={requestMessage}
                  onChange={setRequestMessage}
                  placeholder="例如：你在动态里提到过这家咖啡店"
                />
              </Field>
              <Field
                label="同时推送通知"
                hint="仅「发好友申请」生效（默认开）；关掉后用户要等下次打开 App 才看到申请"
              >
                <label className="admin-row" style={{ fontSize: 13 }}>
                  <input
                    type="checkbox"
                    checked={notifyOnRequest}
                    onChange={(event) => setNotifyOnRequest(event.target.checked)}
                  />
                  <span className="admin-muted">发申请时给用户推一条通知</span>
                </label>
              </Field>
            </div>

            {candidatesQuery.isError ? (
              <ErrorState
                title="读取可指派角色失败"
                message={errorMessage(candidatesQuery.error)}
                issues={
                  candidatesQuery.error instanceof AdminApiError
                    ? candidatesQuery.error.issues
                    : undefined
                }
                onRetry={() => void candidatesQuery.refetch()}
              />
            ) : candidatesQuery.isPending ? (
              <LoadingState label="正在搜索角色…" />
            ) : candidatesQuery.data && candidatesQuery.data.ok === false ? (
              <ErrorState title="角色搜索不可用" issues={candidatesQuery.data.issues} />
            ) : (
              <Table<CandidateRow>
                rows={candidates}
                rowKey={(row) => row.id}
                empty={
                  <EmptyState
                    title={candidateQ ? "没有匹配的角色" : "没有可指派的角色"}
                    description={
                      candidateQ
                        ? "换个关键词，或点「清除」看全部角色。"
                        : "先去「角色工坊」建角色，再回来指派。"
                    }
                  />
                }
                columns={[
                  {
                    key: "persona",
                    header: "角色",
                    render: (row) => (
                      <div className="admin-row">
                        <PersonaAvatar url={row.avatar} name={row.nickname} />
                        <div className="admin-col" style={{ gap: 0 }}>
                          <span>{asText(row.nickname) || "（无名）"}</span>
                          <span className="admin-mono admin-muted">{row.id}</span>
                        </div>
                      </div>
                    ),
                  },
                  {
                    key: "occupation",
                    header: "职业 / 城市",
                    render: (row) => {
                      const text = [asText(row.occupation), asText(row.city)]
                        .filter(Boolean)
                        .join(" · ");
                      return text || null;
                    },
                  },
                  { key: "language", header: "语言", render: (row) => asText(row.language) || null },
                  {
                    key: "hint",
                    header: "匹配说明",
                    render: (row) => (
                      <span className="admin-muted">{asText(row.match_hint) || null}</span>
                    ),
                  },
                  {
                    key: "actions",
                    header: "操作",
                    render: (row) =>
                      row.is_friend ? (
                        <Badge tone="success">已是好友</Badge>
                      ) : (
                        <div className="admin-row">
                          <Button
                            size="sm"
                            loading={assignPending(row.id, "request")}
                            onClick={() => assign(row, "request")}
                            title="给用户推通知，需要用户同意后才会成为好友"
                          >
                            发好友申请
                          </Button>
                          <Button
                            size="sm"
                            variant="danger"
                            loading={assignPending(row.id, "direct")}
                            onClick={() => assign(row, "direct")}
                            title="跳过用户同意，角色立刻出现在用户好友列表里"
                          >
                            直接成为好友
                          </Button>
                        </div>
                      ),
                  },
                ]}
              />
            )}
          </div>
        </>
      )}

      {/* ---------- 解除好友关系：确认 ---------- */}
      <ConfirmDialog
        open={Boolean(removeTarget)}
        title="解除好友关系？"
        danger
        confirmLabel="解除"
        busy={removeMutation.isPending}
        onConfirm={() => {
          if (removeTarget) removeMutation.mutate(removeTarget.id);
        }}
        onCancel={() => setRemoveTarget(null)}
        body={
          <div className="admin-col">
            <p style={{ margin: 0 }}>
              解除后，角色{" "}
              <strong>{asText(removeTarget?.persona_nickname) || asText(removeTarget?.persona_id)}</strong>{" "}
              不再出现在该用户的好友列表里。
            </p>
            <p style={{ margin: 0 }}>角色本身及其它用户的好友关系不受影响。此操作不可撤销。</p>
          </div>
        }
      />
    </section>
  );
}
