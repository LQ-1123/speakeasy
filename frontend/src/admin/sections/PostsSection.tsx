"use client";

/**
 * 动态（`#/world/posts`）：待发队列 / 已发 / 写一条 三个 Tab。
 *
 * 用的接口
 * --------
 * - `GET  /api/admin/posts/pending?offset=&limit=`        待发队列（`post_queue.status='queued'`）
 * - `GET  /api/admin/posts/published?persona_id=&offset=&limit=`  已发（`persona_posts`）
 * - `POST /api/admin/posts` body `{persona_id, content, media?, publish_at?, publish_now?}`
 * - `PUT  /api/admin/posts/{queue_id}`                    编辑待发（`{content, media, publish_at}`）
 * - `DELETE /api/admin/posts/{queue_id}`                  **软取消**（`status='cancelled'`，保留记录）
 * - `POST /api/admin/posts/{queue_id}/publish-now`         立即发布
 * - `GET  /api/admin/posts/personas`                     写动态 / 筛选用的角色列表
 * - `GET  /api/admin/media/library?limit=`               图片选择器（复用 P2 的图库接口）
 *
 * 任务里点名要修的两个旧体验问题
 * ------------------------------
 * 1. **旧版的「取消」是直接删**。这里明确是**软取消**：后端只把 `status` 改成
 *    `cancelled`，记录保留在 `post_queue` 里。所以界面上所有文案都是
 *    「取消（保留记录）」，没有「删除」两个字——避免运营以为自己删干净了。
 * 2. **已发布的动态不可编辑**。后端会返回 `{ok:false, issues:["已发布，不能编辑"]}`，
 *    但如果等运营点了才报错，那已经是一次白跑。所以列表里编辑按钮直接
 *    `disabled` 并在旁边写清原因，而不是让人点了才看到错。
 *
 * 图片字段的形状
 * --------------
 * `persona_posts.media` / `post_queue.media` 是 JSON 数组。不同生成路径写过
 * 纯 URL 字符串，也可能写成 `{url}` 对象，这里用 `mediaUrls()` 两种都吃；
 * 提交时统一发 `string[]`（图库 URL）。
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { AdminApiError, apiDelete, apiGet, apiPost, apiPut, withQuery } from "../api";
import { adminKeys } from "../queryKeys";
import {
  Badge,
  Button,
  ConfirmDialog,
  Drawer,
  EmptyState,
  ErrorState,
  Field,
  ImagePicker,
  LoadingState,
  Pager,
  Segmented,
  Select,
  Table,
  TextArea,
  TextInput,
  ToastStack,
  useToasts,
} from "../ui";

/* -------------------------------------------------------------------------- */
/* 后端数据形状                                                                */
/* -------------------------------------------------------------------------- */

type Tab = "pending" | "published" | "compose";

type ApiRecord = Record<string, unknown>;

interface PersonaOption {
  id: string;
  nickname?: string;
  avatar?: string;
}

interface PendingRow {
  id: string;
  persona_id?: string;
  /** 后端大概率把角色昵称/头像 join 进来；没有也能降级显示 persona_id。 */
  persona_nickname?: string;
  persona_avatar?: string;
  content?: string;
  media?: unknown;
  source_type?: string;
  source_event_id?: string;
  source_event_title?: string;
  status?: string;
  publish_at?: string;
  created_at?: string;
}

interface PublishedRow {
  id: string;
  persona_id?: string;
  persona_nickname?: string;
  persona_avatar?: string;
  content?: string;
  media?: unknown;
  created_at?: string;
  like_count?: number;
  comment_count?: number;
}

interface ListPayload<T> {
  ok?: boolean;
  issues?: string[];
  total?: number;
  offset?: number;
  limit?: number;
  items?: T[];
}

interface PersonasPayload {
  ok?: boolean;
  issues?: string[];
  items?: PersonaOption[];
}

interface MediaItem {
  url?: string;
  title?: string;
}

interface MediaPayload {
  ok?: boolean;
  issues?: string[];
  items?: MediaItem[];
}

const PAGE_SIZE = 20;

/**
 * 内容上限。后端 `event_posts.py::MAX_POST_CHARS = 400` 是动态正文的硬上限；
 * 前端先按同一个数字给字数提示，避免"写完才被拒"。
 */
const CONTENT_LIMIT = 400;

const SOURCE_TYPE_LABEL: Record<string, string> = {
  manual: "手动创建",
  event: "事件触发",
  life: "生活事件",
  daily: "每日世界",
  auto: "自动生成",
};

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

/** `media` 可能是 `["/uploads/a.jpg"]` 或 `[{url:"/uploads/a.jpg"}]`，两种都吃。 */
function mediaUrls(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const out: string[] = [];
  for (const item of value) {
    if (typeof item === "string" && item.trim()) {
      out.push(item.trim());
      continue;
    }
    if (item && typeof item === "object") {
      const row = item as ApiRecord;
      const url = asText(row.url) || asText(row.path) || asText(row.src);
      if (url) out.push(url);
    }
  }
  return out;
}

function sourceLabel(row: PendingRow): string {
  const type = asText(row.source_type);
  return SOURCE_TYPE_LABEL[type] || type || "未知来源";
}

function sourceDetail(row: PendingRow): string {
  return asText(row.source_event_title) || asText(row.source_event_id);
}

/* -------------------------------------------------------------------------- */
/* 局部展示组件                                                                */
/* -------------------------------------------------------------------------- */

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

function PersonaCell({ nickname, avatar, id }: { nickname?: string; avatar?: string; id?: string }) {
  return (
    <div className="admin-row">
      <PersonaAvatar url={avatar} name={nickname} />
      <div className="admin-col" style={{ gap: 0 }}>
        <span>{asText(nickname) || "（角色已删除）"}</span>
        <span className="admin-mono admin-muted">{asText(id) || "—"}</span>
      </div>
    </div>
  );
}

function ContentSummary({ content, media }: { content?: string; media?: unknown }) {
  const text = asText(content);
  const urls = mediaUrls(media);
  return (
    <div className="admin-col" style={{ gap: 2 }}>
      <span
        className="admin-truncate"
        style={{ maxWidth: 340, display: "inline-block" }}
        title={text}
      >
        {text || "（空内容）"}
      </span>
      {urls.length ? <Badge tone="info">图文 · {urls.length} 张图</Badge> : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 分区                                                                        */
/* -------------------------------------------------------------------------- */

export default function PostsSection() {
  const queryClient = useQueryClient();
  const { toasts, push } = useToasts();

  const [tab, setTab] = useState<Tab>("pending");

  /* 待发队列 */
  const [pendingOffset, setPendingOffset] = useState(0);
  const [editTarget, setEditTarget] = useState<PendingRow | null>(null);
  const [editContent, setEditContent] = useState("");
  const [editPublishAt, setEditPublishAt] = useState("");
  const [cancelTarget, setCancelTarget] = useState<PendingRow | null>(null);
  const [publishTarget, setPublishTarget] = useState<PendingRow | null>(null);

  /* 已发 */
  const [publishedOffset, setPublishedOffset] = useState(0);
  const [publishedPersona, setPublishedPersona] = useState("");

  /* 写一条 */
  const [personaId, setPersonaId] = useState("");
  const [content, setContent] = useState("");
  const [mediaUrl, setMediaUrl] = useState("");
  const [mediaInput, setMediaInput] = useState("");
  const [publishAt, setPublishAt] = useState("");

  /* ------------------------------ 查询 ------------------------------ */

  const personasQuery = useQuery({
    queryKey: ["admin", "posts", "personas"],
    queryFn: () => apiGet<PersonasPayload>("/api/admin/posts/personas"),
  });
  const personas = personasQuery.data?.items ?? [];

  const pendingQuery = useQuery({
    queryKey: [...adminKeys.postsPending(), pendingOffset, PAGE_SIZE],
    queryFn: () =>
      apiGet<ListPayload<PendingRow>>(
        withQuery("/api/admin/posts/pending", { offset: pendingOffset, limit: PAGE_SIZE }),
      ),
    enabled: tab === "pending",
  });

  const publishedQuery = useQuery({
    queryKey: adminKeys.postsPublished({
      persona_id: publishedPersona,
      offset: publishedOffset,
      limit: PAGE_SIZE,
    }),
    queryFn: () =>
      apiGet<ListPayload<PublishedRow>>(
        withQuery("/api/admin/posts/published", {
          persona_id: publishedPersona,
          offset: publishedOffset,
          limit: PAGE_SIZE,
        }),
      ),
    enabled: tab === "published",
  });

  const mediaQuery = useQuery({
    queryKey: adminKeys.mediaLibrary({ limit: 48, purpose: "posts-compose" }),
    queryFn: () =>
      apiGet<MediaPayload>(withQuery("/api/admin/media/library", { limit: 48 })),
    enabled: tab === "compose",
  });

  const galleryImages = (mediaQuery.data?.items ?? [])
    .map((item) => ({ url: asText(item.url), label: asText(item.title) }))
    .filter((item) => item.url);

  /* ------------------------------ 失效 ------------------------------ */

  const invalidatePosts = () => {
    // 前缀失效：待发 / 已发的所有分页与筛选变体都一起刷新。
    void queryClient.invalidateQueries({ queryKey: adminKeys.postsPending() });
    void queryClient.invalidateQueries({ queryKey: ["admin", "posts", "published"] });
  };

  /* ------------------------------ 写操作 ------------------------------ */

  const createMutation = useMutation({
    mutationFn: async (request: { payload: ApiRecord; publishNow: boolean }) =>
      ensureOk(await apiPost<ApiRecord>("/api/admin/posts", request.payload)),
    onSuccess: (result, request) => {
      invalidatePosts();
      setPersonaId("");
      setContent("");
      setMediaUrl("");
      setMediaInput("");
      setPublishAt("");
      setTab(request.publishNow ? "published" : "pending");
      if (request.publishNow) {
        push(`已立即发布${asText(result.post_id) ? `（post ${asText(result.post_id)}）` : ""}`);
      } else {
        push("已存入待发队列（不会立刻发布）");
      }
    },
    onError: (error) => push(errorMessage(error), "error"),
  });

  const updateMutation = useMutation({
    mutationFn: async (request: {
      queueId: string;
      content: string;
      media: string[];
      publish_at: string;
    }) =>
      ensureOk(
        await apiPut<ApiRecord>(`/api/admin/posts/${encodeURIComponent(request.queueId)}`, {
          content: request.content,
          media: request.media,
          publish_at: request.publish_at,
        }),
      ),
    onSuccess: () => {
      setEditTarget(null);
      invalidatePosts();
      push("已保存待发内容");
    },
    onError: (error) => push(errorMessage(error), "error"),
  });

  const cancelMutation = useMutation({
    mutationFn: async (queueId: string) =>
      ensureOk(await apiDelete<ApiRecord>(`/api/admin/posts/${encodeURIComponent(queueId)}`)),
    onSuccess: () => {
      setCancelTarget(null);
      invalidatePosts();
      push("已取消（记录已保留，状态改为 cancelled，不会再发布）");
    },
    onError: (error) => {
      setCancelTarget(null);
      push(errorMessage(error), "error");
    },
  });

  const publishNowMutation = useMutation({
    mutationFn: async (queueId: string) =>
      ensureOk(
        await apiPost<ApiRecord>(`/api/admin/posts/${encodeURIComponent(queueId)}/publish-now`),
      ),
    onSuccess: () => {
      setPublishTarget(null);
      invalidatePosts();
      setTab("published");
      push("已立即发布：现在可以在「已发」里看到");
    },
    onError: (error) => {
      setPublishTarget(null);
      push(errorMessage(error), "error");
    },
  });

  /* ------------------------------ 交互 ------------------------------ */

  const trimmedContent = content.trim();
  const overLimit = trimmedContent.length > CONTENT_LIMIT;
  const canSubmit = Boolean(personaId) && trimmedContent.length > 0 && !overLimit;

  const submitCompose = (publishNow: boolean) => {
    if (!personaId) {
      push("请先选择一个角色", "error");
      return;
    }
    if (!trimmedContent) {
      push("内容不能为空", "error");
      return;
    }
    if (overLimit) {
      push(`内容超过 ${CONTENT_LIMIT} 字上限（当前 ${trimmedContent.length} 字）`, "error");
      return;
    }
    const payload: ApiRecord = {
      persona_id: personaId,
      content: trimmedContent,
      publish_now: publishNow,
    };
    const media = mediaUrl.trim();
    if (media) payload.media = [media];
    const scheduled = publishAt.trim();
    if (!publishNow && scheduled) payload.publish_at = scheduled;
    createMutation.mutate({ payload, publishNow });
  };

  const openEdit = (row: PendingRow) => {
    setEditTarget(row);
    setEditContent(asText(row.content));
    setEditPublishAt(asText(row.publish_at));
  };

  const saveEdit = () => {
    if (!editTarget) return;
    const text = editContent.trim();
    if (!text) {
      push("内容不能为空", "error");
      return;
    }
    if (text.length > CONTENT_LIMIT) {
      push(`内容超过 ${CONTENT_LIMIT} 字上限（当前 ${text.length} 字）`, "error");
      return;
    }
    updateMutation.mutate({
      queueId: editTarget.id,
      content: text,
      media: mediaUrls(editTarget.media),
      publish_at: editPublishAt.trim(),
    });
  };

  const pendingData = pendingQuery.data;
  const publishedData = publishedQuery.data;
  const pendingRows = pendingData?.items ?? [];
  const publishedRows = publishedData?.items ?? [];

  const personaOptions: Array<{ value: string; label: string }> = [
    { value: "", label: "全部角色" },
    ...personas.map((persona) => ({
      value: persona.id,
      label: `${asText(persona.nickname) || persona.id} · ${persona.id}`,
    })),
  ];

  const composePersonaOptions: Array<{ value: string; label: string }> = [
    { value: "", label: "选择一个角色…" },
    ...personas.map((persona) => ({
      value: persona.id,
      label: `${asText(persona.nickname) || persona.id} · ${persona.id}`,
    })),
  ];

  /* ------------------------------ 渲染 ------------------------------ */

  return (
    <section className="admin-section">
      <ToastStack toasts={toasts} />

      <div className="admin-section-head">
        <div>
          <h1 className="admin-section-title">动态</h1>
          <p className="admin-section-desc">
            待发队列是"还没出现在用户信息流里"的动态：可以编辑、软取消、或立刻发布。
            已发布的动态直接进入用户信息流，<strong>不可编辑</strong>。
          </p>
        </div>
        <div className="admin-section-actions">
          <Button
            onClick={() => {
              void personasQuery.refetch();
              if (tab === "pending") void pendingQuery.refetch();
              if (tab === "published") void publishedQuery.refetch();
              if (tab === "compose") void mediaQuery.refetch();
            }}
          >
            刷新
          </Button>
        </div>
      </div>

      <div className="admin-row">
        <Segmented<Tab>
          value={tab}
          onChange={setTab}
          options={[
            { value: "pending", label: "待发队列" },
            { value: "published", label: "已发" },
            { value: "compose", label: "写一条" },
          ]}
        />
        <span className="admin-muted">
          取消是<strong>软取消</strong>：保留记录、只改状态，不等于删除。
        </span>
      </div>

      {personasQuery.isError ? (
        <ErrorState
          title="读取角色列表失败"
          message={errorMessage(personasQuery.error)}
          issues={personasQuery.error instanceof AdminApiError ? personasQuery.error.issues : undefined}
          onRetry={() => void personasQuery.refetch()}
        />
      ) : personasQuery.data && personasQuery.data.ok === false ? (
        <ErrorState title="角色列表不可用" issues={personasQuery.data.issues} />
      ) : null}

      {/* ================= 待发队列 ================= */}
      {tab === "pending" ? (
        <>
          <div className="admin-card">
            <div className="admin-card-head">
              <h2 className="admin-card-title">待发队列</h2>
              <span className="admin-card-sub">
                共 {asNumber(pendingData?.total ?? pendingRows.length)} 条 · 只有发布后才会出现在用户信息流
              </span>
            </div>

            {pendingQuery.isError ? (
              <ErrorState
                title="读取待发队列失败"
                message={errorMessage(pendingQuery.error)}
                issues={
                  pendingQuery.error instanceof AdminApiError ? pendingQuery.error.issues : undefined
                }
                onRetry={() => void pendingQuery.refetch()}
              />
            ) : pendingQuery.isPending ? (
              <LoadingState label="正在读取待发队列…" />
            ) : pendingData && pendingData.ok === false ? (
              <ErrorState title="待发队列不可用" issues={pendingData.issues} />
            ) : (
              <>
                <Table<PendingRow>
                  rows={pendingRows}
                  rowKey={(row) => row.id}
                  empty={
                    <EmptyState
                      title="待发队列是空的"
                      description="所有排期内容都已发布或取消。可以切到「写一条」手动排一条。"
                      action={<Button onClick={() => setTab("compose")}>写一条</Button>}
                    />
                  }
                  columns={[
                    {
                      key: "persona",
                      header: "角色",
                      render: (row) => (
                        <PersonaCell
                          nickname={row.persona_nickname}
                          avatar={row.persona_avatar}
                          id={row.persona_id}
                        />
                      ),
                    },
                    {
                      key: "content",
                      header: "内容",
                      render: (row) => (
                        <ContentSummary content={row.content} media={row.media} />
                      ),
                    },
                    {
                      key: "publish_at",
                      header: "计划发布时间",
                      render: (row) =>
                        formatTime(row.publish_at) || (
                          <span className="admin-muted">未设定</span>
                        ),
                    },
                    {
                      key: "source",
                      header: "来源事件",
                      render: (row) => (
                        <div className="admin-col" style={{ gap: 0 }}>
                          <span>{sourceLabel(row)}</span>
                          {sourceDetail(row) ? (
                            <span className="admin-mono admin-muted">{sourceDetail(row)}</span>
                          ) : null}
                        </div>
                      ),
                    },
                    {
                      key: "actions",
                      header: "操作",
                      render: (row) => (
                        <div className="admin-row">
                          <Button
                            size="sm"
                            disabled={cancelMutation.isPending || publishNowMutation.isPending}
                            onClick={() => openEdit(row)}
                          >
                            编辑
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            loading={cancelMutation.isPending && cancelMutation.variables === row.id}
                            onClick={() => setCancelTarget(row)}
                            title="软取消：保留记录，状态改为 cancelled，不再发布"
                          >
                            取消（保留记录）
                          </Button>
                          <Button
                            size="sm"
                            variant="primary"
                            loading={
                              publishNowMutation.isPending && publishNowMutation.variables === row.id
                            }
                            onClick={() => setPublishTarget(row)}
                          >
                            立即发布
                          </Button>
                        </div>
                      ),
                    },
                  ]}
                />
                <Pager
                  total={asNumber(pendingData?.total ?? pendingRows.length)}
                  offset={pendingOffset}
                  limit={PAGE_SIZE}
                  onChange={(next) => setPendingOffset(next)}
                />
                <p className="admin-muted" style={{ margin: 0 }}>
                  「取消（保留记录）」是软取消：只把状态改成{" "}
                  <span className="admin-mono">cancelled</span>，记录仍在队列里可查，不会发布到用户信息流。
                </p>
              </>
            )}
          </div>
        </>
      ) : null}

      {/* ================= 已发 ================= */}
      {tab === "published" ? (
        <div className="admin-card">
          <div className="admin-card-head">
            <h2 className="admin-card-title">已发动态</h2>
            <span className="admin-card-sub">
              共 {asNumber(publishedData?.total ?? publishedRows.length)} 条 · 已进入用户信息流
            </span>
            <div style={{ marginLeft: "auto", minWidth: 200 }}>
              <Select<string>
                value={publishedPersona}
                onChange={(value) => {
                  setPublishedPersona(value);
                  setPublishedOffset(0);
                }}
                options={personaOptions}
              />
            </div>
          </div>

          {publishedQuery.isError ? (
            <ErrorState
              title="读取已发动态失败"
              message={errorMessage(publishedQuery.error)}
              issues={
                publishedQuery.error instanceof AdminApiError
                  ? publishedQuery.error.issues
                  : undefined
              }
              onRetry={() => void publishedQuery.refetch()}
            />
          ) : publishedQuery.isPending ? (
            <LoadingState label="正在读取已发动态…" />
          ) : publishedData && publishedData.ok === false ? (
            <ErrorState title="已发动态不可用" issues={publishedData.issues} />
          ) : (
            <>
              <Table<PublishedRow>
                rows={publishedRows}
                rowKey={(row) => row.id}
                empty={
                  <EmptyState
                    title={publishedPersona ? "这个角色还没有已发动态" : "还没有已发动态"}
                    description={
                      publishedPersona
                        ? "换个角色筛选，或先去「待发队列」发布一条。"
                        : "待发队列里的动态发布后会出现在这里。"
                    }
                  />
                }
                columns={[
                  {
                    key: "persona",
                    header: "角色",
                    render: (row) => (
                      <PersonaCell
                        nickname={row.persona_nickname}
                        avatar={row.persona_avatar}
                        id={row.persona_id}
                      />
                    ),
                  },
                  {
                    key: "content",
                    header: "内容",
                    render: (row) => (
                      <ContentSummary content={row.content} media={row.media} />
                    ),
                  },
                  {
                    key: "created_at",
                    header: "发布时间",
                    render: (row) => formatTime(row.created_at) || null,
                  },
                  {
                    key: "likes",
                    header: "点赞",
                    numeric: true,
                    width: "72px",
                    render: (row) => asNumber(row.like_count),
                  },
                  {
                    key: "comments",
                    header: "评论",
                    numeric: true,
                    width: "72px",
                    render: (row) => asNumber(row.comment_count),
                  },
                  {
                    key: "actions",
                    header: "操作",
                    render: () => (
                      <div className="admin-col" style={{ gap: 2, alignItems: "flex-start" }}>
                        <Button
                          size="sm"
                          disabled
                          title="已发布，不能编辑：内容已经进入用户信息流"
                        >
                          编辑
                        </Button>
                        <span className="admin-muted" style={{ fontSize: 12 }}>
                          已发布，不能编辑
                        </span>
                      </div>
                    ),
                  },
                ]}
              />
              <Pager
                total={asNumber(publishedData?.total ?? publishedRows.length)}
                offset={publishedOffset}
                limit={PAGE_SIZE}
                onChange={(next) => setPublishedOffset(next)}
              />
              <p className="admin-muted" style={{ margin: 0 }}>
                已发布的动态不可编辑：内容已经进入用户信息流，改了会与用户已看到的内容不一致，
                后端也会直接拒绝。需要改内容请从待发队列重新排一条。
              </p>
            </>
          )}
        </div>
      ) : null}

      {/* ================= 写一条 ================= */}
      {tab === "compose" ? (
        <div className="admin-card">
          <div className="admin-card-head">
            <h2 className="admin-card-title">写一条动态</h2>
            <span className="admin-card-sub">
              「存为待发」只入队不发布；「立即发布」直接进用户信息流，且之后不可编辑
            </span>
          </div>

          <div className="admin-form-grid">
            <Field label="角色" htmlFor="post-persona" hint="动态会以这个角色的身份出现在用户信息流里">
              <Select<string>
                id="post-persona"
                value={personaId}
                onChange={setPersonaId}
                options={composePersonaOptions}
                disabled={personasQuery.isPending}
              />
            </Field>
            <Field
              label={`内容（${trimmedContent.length} / ${CONTENT_LIMIT} 字）`}
              htmlFor="post-content"
              hint="超过上限后端会拒绝，所以这里先给字数提示"
              error={overLimit ? `超过 ${CONTENT_LIMIT} 字上限` : undefined}
            >
              <TextArea
                id="post-content"
                rows={6}
                value={content}
                onChange={setContent}
                placeholder="今天在这家咖啡店坐了一下午，窗外一直在下雨。"
                invalid={overLimit}
              />
            </Field>
          </div>

          <Field
            label="图片（可选）"
            hint="从图库选一张，或直接填一个图片 URL；动态最多展示一张图"
          >
            <div className="admin-col">
              {mediaUrl ? (
                <div className="admin-row">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={mediaUrl}
                    alt="已选图片"
                    loading="lazy"
                    style={{ width: 72, height: 72, objectFit: "cover", borderRadius: 6 }}
                  />
                  <Button size="sm" variant="ghost" onClick={() => setMediaUrl("")}>
                    移除图片
                  </Button>
                  <span className="admin-mono admin-muted">{mediaUrl}</span>
                </div>
              ) : (
                <p className="admin-muted" style={{ margin: 0 }}>
                  未选图片：这条会是纯文字动态。
                </p>
              )}

              <div className="admin-row">
                <div style={{ flex: "1 1 260px" }}>
                  <TextInput
                    value={mediaInput}
                    onChange={setMediaInput}
                    placeholder="图片 URL，例如 /uploads/abc.jpg"
                    onEnter={() => {
                      const url = mediaInput.trim();
                      if (!url) {
                        push("请先填图片 URL", "error");
                        return;
                      }
                      setMediaUrl(url);
                    }}
                  />
                </div>
                <Button
                  size="sm"
                  onClick={() => {
                    const url = mediaInput.trim();
                    if (!url) {
                      push("请先填图片 URL", "error");
                      return;
                    }
                    setMediaUrl(url);
                  }}
                >
                  使用该 URL
                </Button>
              </div>

              {mediaQuery.isError ? (
                <ErrorState
                  title="读取图库失败"
                  message={errorMessage(mediaQuery.error)}
                  onRetry={() => void mediaQuery.refetch()}
                />
              ) : mediaQuery.isPending ? (
                <LoadingState label="正在读取图库…" />
              ) : (
                <ImagePicker
                  images={galleryImages}
                  selected={mediaUrl}
                  onSelect={(url) => {
                    setMediaUrl(url);
                    setMediaInput(url);
                  }}
                  onRefresh={() => void mediaQuery.refetch()}
                />
              )}
            </div>
          </Field>

          <div className="admin-form-grid">
            <Field
              label="发布时间（可选）"
              htmlFor="post-publish-at"
              hint="留空 = 立即；填了会作为计划发布时间写进队列（「立即发布」按钮会忽略它）"
            >
              <TextInput
                id="post-publish-at"
                value={publishAt}
                onChange={setPublishAt}
                placeholder="例如 2026-11-12 18:00"
              />
            </Field>
          </div>

          <div className="admin-row">
            <Button
              loading={createMutation.isPending && createMutation.variables?.publishNow === false}
              disabled={!canSubmit || createMutation.isPending}
              onClick={() => submitCompose(false)}
            >
              存为待发
            </Button>
            <Button
              variant="primary"
              loading={createMutation.isPending && createMutation.variables?.publishNow === true}
              disabled={!canSubmit || createMutation.isPending}
              onClick={() => submitCompose(true)}
            >
              立即发布
            </Button>
            <span className="admin-muted">
              立即发布后不可编辑；不确定就先「存为待发」，到队列里再发布。
            </span>
          </div>
        </div>
      ) : null}

      {/* ---------- 编辑待发：抽屉 ---------- */}
      <Drawer
        open={Boolean(editTarget)}
        title={`编辑待发：${asText(editTarget?.persona_nickname) || asText(editTarget?.persona_id) || ""}`}
        onClose={() => setEditTarget(null)}
        footer={
          <>
            <Button variant="primary" loading={updateMutation.isPending} onClick={saveEdit}>
              保存
            </Button>
            <Button variant="ghost" onClick={() => setEditTarget(null)} disabled={updateMutation.isPending}>
              取消
            </Button>
          </>
        }
      >
        <div className="admin-col">
          <Field
            label={`内容（${editContent.trim().length} / ${CONTENT_LIMIT} 字）`}
            htmlFor="edit-content"
            error={editContent.trim().length > CONTENT_LIMIT ? `超过 ${CONTENT_LIMIT} 字上限` : undefined}
          >
            <TextArea
              id="edit-content"
              rows={6}
              value={editContent}
              onChange={setEditContent}
              invalid={editContent.trim().length > CONTENT_LIMIT}
            />
          </Field>
          <Field
            label="计划发布时间"
            htmlFor="edit-publish-at"
            hint="留空表示交给排期；格式 YYYY-MM-DD HH:MM"
          >
            <TextInput
              id="edit-publish-at"
              value={editPublishAt}
              onChange={setEditPublishAt}
              placeholder="例如 2026-11-12 18:00"
            />
          </Field>
          {mediaUrls(editTarget?.media).length ? (
            <p className="admin-muted" style={{ margin: 0 }}>
              这条动态已有 {mediaUrls(editTarget?.media).length} 张图，编辑只改文字与时间，图片沿用原值。
            </p>
          ) : null}
        </div>
      </Drawer>

      {/* ---------- 软取消：确认 ---------- */}
      <ConfirmDialog
        open={Boolean(cancelTarget)}
        title="取消这条待发动态？"
        confirmLabel="取消（保留记录）"
        busy={cancelMutation.isPending}
        onConfirm={() => {
          if (cancelTarget) cancelMutation.mutate(cancelTarget.id);
        }}
        onCancel={() => setCancelTarget(null)}
        body={
          <div className="admin-col">
            <p style={{ margin: 0 }}>
              这是一次<strong>软取消</strong>：记录会保留，状态改为{" "}
              <span className="admin-mono">cancelled</span>，这条动态不会再被发布。
            </p>
            <p style={{ margin: 0 }}>
              不会从数据库删除，也不会影响已经发布的内容。之后想再发一条，请到「写一条」重新排。
            </p>
          </div>
        }
      />

      {/* ---------- 立即发布：确认 ---------- */}
      <ConfirmDialog
        open={Boolean(publishTarget)}
        title="立即发布这条动态？"
        confirmLabel="立即发布"
        busy={publishNowMutation.isPending}
        onConfirm={() => {
          if (publishTarget) publishNowMutation.mutate(publishTarget.id);
        }}
        onCancel={() => setPublishTarget(null)}
        body={
          <div className="admin-col">
            <p style={{ margin: 0 }}>
              发布后这条动态会立刻出现在该角色的用户信息流里，用户马上能看到。
            </p>
            <p style={{ margin: 0 }}>
              <strong>发布后不可编辑、不可撤回</strong>（后端会拒绝编辑已发布内容）。确认内容与配图无误再继续。
            </p>
          </div>
        }
      />
    </section>
  );
}
