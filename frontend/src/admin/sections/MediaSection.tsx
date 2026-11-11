"use client";

/**
 * 图片素材：图库、搜图入库、按兴趣补图、清理。
 *
 * 三条运营必须知道的规则（直接写在页面上，否则会误操作）：
 * 1. **图库有配额**（默认 200MB / 800 张），超出后新的图进不来。
 * 2. **有 45 天过期**，但**被动态引用的图永不删除**——宁可超配额也不弄坏线上动态。
 * 3. **清理只删「没被引用」的图**，所以「可清理」数量和「未引用」数量是同一个口径。
 */

import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { AdminApiError, apiGet, apiPost, apiUpload, withQuery } from "../api";
import { adminKeys } from "../queryKeys";
import {
  Badge,
  Button,
  ConfirmDialog,
  ErrorState,
  Field,
  LoadingState,
  Pager,
  Table,
  TextInput,
  useToasts,
  ToastStack,
} from "../ui";

interface MediaStats {
  ok?: boolean;
  dir?: string;
  files: number;
  bytes: number;
  bytes_mb: number;
  referenced_files: number;
  referenced_bytes_mb: number;
  unreferenced_files: number;
  db_rows?: number;
  oldest_fetched_at?: string;
  limits?: { max_bytes: number; max_files: number; ttl_days: number };
}

interface LibraryItem {
  path: string;
  url: string;
  title: string;
  source: string;
  license: string;
  author: string;
  fetched_at: string;
  persona_id: string;
  persona_nickname: string;
  referenced: boolean;
  size: number;
}

interface LibraryPayload {
  ok: boolean;
  total: number;
  offset: number;
  limit: number;
  items: LibraryItem[];
  stats?: { files: number; referenced_files: number; unreferenced_files: number };
  issues?: string[];
}

interface SearchItem {
  external_id: string;
  source: string;
  title: string;
  tags: string[];
  license: string;
  author: string;
  page_url: string;
  thumb_url: string;
  downloaded: boolean;
}

const PAGE_SIZE = 24;

/**
 * 上传本地图片。
 *
 * 与「搜图入库」的区别要说清楚：搜图是**从图源找**（带版权信息），
 * 上传是**运营自己的图**（版权归自己，来源记作 manual）。
 * 文件名由后端重命名——不信任上传文件名是防路径穿越的基本要求。
 */
function UploadCard({ onUploaded }: { onUploaded: () => void }) {
  const { toasts, push } = useToasts();
  const inputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [result, setResult] = useState<{ path?: string; url?: string; size?: number } | null>(null);

  const upload = async () => {
    const file = inputRef.current?.files?.[0];
    if (!file) {
      push("请选择一张图片", "error");
      return;
    }
    const form = new FormData();
    form.append("file", file);
    setUploading(true);
    try {
      const response = await apiUpload<{
        ok?: boolean;
        issues?: string[];
        path?: string;
        url?: string;
        size?: number;
      }>("/api/admin/media/upload", form);
      if (response.ok === false) {
        push((response.issues ?? ["上传失败"]).join("；"), "error");
        setResult(null);
      } else {
        setResult(response);
        push("已上传并入库");
        if (inputRef.current) inputRef.current.value = "";
        onUploaded();
      }
    } catch (cause) {
      const err = cause as AdminApiError;
      push(err.issues?.join("；") || err.detail || String(cause), "error");
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="admin-card">
      <ToastStack toasts={toasts} />
      <div className="admin-card-head">
        <h2 className="admin-card-title">上传图片</h2>
        <span className="admin-card-sub">自己的图（版权归你，来源记为 manual）</span>
      </div>
      <div className="admin-row">
        <div style={{ flex: "1 1 240px" }}>
          <input ref={inputRef} type="file" accept="image/*" className="admin-input" />
        </div>
        <Button variant="primary" onClick={() => void upload()} loading={uploading}>
          上传
        </Button>
      </div>
      {result?.url ? (
        <div className="admin-row" style={{ marginTop: 10, alignItems: "center" }}>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={result.url}
            alt="上传结果"
            style={{ width: 72, height: 72, objectFit: "cover", borderRadius: 6 }}
          />
          <span className="admin-mono admin-muted">{result.path}</span>
        </div>
      ) : null}
    </div>
  );
}

export default function MediaSection() {
  const { toasts, push } = useToasts();
  const [offset, setOffset] = useState(0);
  const [onlyUnused, setOnlyUnused] = useState(false);

  const [searchQuery, setSearchQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [searchItems, setSearchItems] = useState<SearchItem[]>([]);
  const [fetchingId, setFetchingId] = useState("");

  const [fillPersonaIds, setFillPersonaIds] = useState("");
  const [fillPerPersona, setFillPerPersona] = useState("1");
  const [filling, setFilling] = useState(false);

  const [confirmPrune, setConfirmPrune] = useState(false);
  const [pruneDryRun, setPruneDryRun] = useState(true);
  const [pruning, setPruning] = useState(false);
  const [pruneResult, setPruneResult] = useState<Record<string, unknown> | null>(null);

  const statsQuery = useQuery({
    queryKey: adminKeys.mediaStats(),
    queryFn: () => apiGet<MediaStats>("/api/admin/media/stats"),
  });

  const libraryQuery = useQuery({
    queryKey: adminKeys.mediaLibrary({ offset, limit: PAGE_SIZE, onlyUnused }),
    queryFn: () =>
      apiGet<LibraryPayload>(
        withQuery("/api/admin/media/library", {
          offset,
          limit: PAGE_SIZE,
          only_unused: onlyUnused,
        }),
      ),
  });

  const refreshAll = () => {
    void statsQuery.refetch();
    void libraryQuery.refetch();
  };

  const runSearch = async () => {
    const query = searchQuery.trim();
    if (!query) {
      push("请输入搜索词", "error");
      return;
    }
    setSearching(true);
    try {
      const result = await apiGet<{ ok?: boolean; items?: SearchItem[]; issues?: string[] }>(
        withQuery("/api/admin/media/search", { query, limit: 8 }),
      );
      if (result.ok === false) {
        push((result.issues ?? ["搜索失败"]).join("；"), "error");
        setSearchItems([]);
      } else {
        setSearchItems(result.items ?? []);
      }
    } catch (cause) {
      push(cause instanceof AdminApiError ? cause.detail : String(cause), "error");
    } finally {
      setSearching(false);
    }
  };

  const fetchOne = async (item: SearchItem) => {
    setFetchingId(item.external_id);
    try {
      const result = await apiPost<{ ok?: boolean; saved?: unknown[]; issues?: string[] }>(
        "/api/admin/media/fetch",
        { query: searchQuery.trim(), external_id: item.external_id, limit: 1 },
      );
      if (result.ok === false) {
        push((result.issues ?? ["入库失败"]).join("；"), "error");
      } else {
        push(`已入库 ${(result.saved ?? []).length} 张`);
        setSearchItems((current) =>
          current.map((row) =>
            row.external_id === item.external_id ? { ...row, downloaded: true } : row,
          ),
        );
        refreshAll();
      }
    } catch (cause) {
      push(cause instanceof AdminApiError ? cause.detail : String(cause), "error");
    } finally {
      setFetchingId("");
    }
  };

  const runFill = async () => {
    const ids = fillPersonaIds
      .split(/[,\s]+/)
      .map((value) => value.trim())
      .filter(Boolean);
    if (!ids.length) {
      push("请填写角色 id（逗号分隔）；留空表示按兴趣自动挑", "error");
      return;
    }
    setFilling(true);
    try {
      const result = await apiPost<{ ok?: boolean; issues?: string[]; filled?: unknown[] }>(
        "/api/admin/media/fill",
        {
          persona_ids: ids,
          per_persona: Number(fillPerPersona) || 1,
          with_caption: true,
        },
      );
      if (result.ok === false) {
        push((result.issues ?? ["补图失败"]).join("；"), "error");
      } else {
        push("补图完成");
        refreshAll();
      }
    } catch (cause) {
      push(cause instanceof AdminApiError ? cause.detail : String(cause), "error");
    } finally {
      setFilling(false);
    }
  };

  const runPrune = async () => {
    setPruning(true);
    try {
      const result = await apiPost<Record<string, unknown>>("/api/admin/media/prune", {
        dry_run: pruneDryRun,
      });
      setPruneResult(result);
      push(pruneDryRun ? "已试算（未删除任何文件）" : "清理完成");
      if (!pruneDryRun) refreshAll();
    } catch (cause) {
      push(cause instanceof AdminApiError ? cause.detail : String(cause), "error");
    } finally {
      setPruning(false);
      setConfirmPrune(false);
    }
  };

  const stats = statsQuery.data;
  const library = libraryQuery.data;

  return (
    <section className="admin-section">
      <ToastStack toasts={toasts} />
      <div className="admin-section-head">
        <div>
          <h1 className="admin-section-title">图片素材</h1>
          <p className="admin-section-desc">
            图库里的图会被角色动态引用。<strong>被引用的图永不自动删除</strong>——宁可超配额也不弄坏线上动态。
          </p>
        </div>
        <div className="admin-section-actions">
          <Button onClick={refreshAll} disabled={statsQuery.isFetching || libraryQuery.isFetching}>
            刷新
          </Button>
        </div>
      </div>

      {statsQuery.isError ? (
        <ErrorState
          title="读取图库统计失败"
          message={
            statsQuery.error instanceof AdminApiError
              ? statsQuery.error.detail
              : String(statsQuery.error)
          }
          onRetry={() => void statsQuery.refetch()}
        />
      ) : stats ? (
        <div className="admin-stat-grid">
          <div className="admin-stat">
            <div className="admin-stat-label">文件数</div>
            <div className="admin-stat-value">{stats.files}</div>
            <div className="admin-stat-hint">
              配额 {stats.limits?.max_files ?? "?"} 张
            </div>
          </div>
          <div className="admin-stat">
            <div className="admin-stat-label">占用</div>
            <div className="admin-stat-value" style={{ fontSize: 20 }}>
              {stats.bytes_mb} MB
            </div>
            <div className="admin-stat-hint">
              配额 {(stats.limits?.max_bytes ?? 0) / 1024 / 1024} MB
            </div>
          </div>
          <div className="admin-stat">
            <div className="admin-stat-label">被引用</div>
            <div className="admin-stat-value">{stats.referenced_files}</div>
            <div className="admin-stat-hint">这些永远不会被自动删</div>
          </div>
          <div className="admin-stat">
            <div className="admin-stat-label">未被引用</div>
            <div className="admin-stat-value">{stats.unreferenced_files}</div>
            <div className="admin-stat-hint">
              过期 {stats.limits?.ttl_days ?? "?"} 天后可清理
            </div>
          </div>
        </div>
      ) : null}

      {/* 搜图入库 ------------------------------------------------------------ */}
      <div className="admin-card">
        <div className="admin-card-head">
          <h2 className="admin-card-title">搜图入库</h2>
          <span className="admin-card-sub">来自 Wikimedia / Openverse（有版权信息）</span>
        </div>
        <div className="admin-row">
          <div style={{ flex: "1 1 240px" }}>
            <TextInput
              value={searchQuery}
              onChange={setSearchQuery}
              placeholder="例如 coffee cup, rainy window"
              onEnter={() => void runSearch()}
            />
          </div>
          <Button onClick={() => void runSearch()} loading={searching}>
            搜索
          </Button>
        </div>
        {searchItems.length ? (
          <div className="admin-img-grid" style={{ marginTop: 12 }}>
            {searchItems.map((item) => (
              <div key={item.external_id} style={{ position: "relative" }}>
                <button
                  type="button"
                  className="admin-img-cell"
                  title={`${item.title}\n${item.license}${item.author ? ` · ${item.author}` : ""}`}
                  onClick={() => void fetchOne(item)}
                  disabled={fetchingId === item.external_id}
                >
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={item.thumb_url} alt={item.title} loading="lazy" />
                  <span className="admin-img-cell-label">
                    {item.downloaded ? "已在库" : fetchingId === item.external_id ? "入库中…" : "点击入库"}
                  </span>
                </button>
              </div>
            ))}
          </div>
        ) : null}
      </div>

      {/* 上传 --------------------------------------------------------------- */}
      <UploadCard onUploaded={refreshAll} />

      {/* 按兴趣补图 ---------------------------------------------------------- */}
      <div className="admin-card">
        <div className="admin-card-head">
          <h2 className="admin-card-title">按角色补图</h2>
          <span className="admin-card-sub">给这些角色各补几张符合其兴趣的图</span>
        </div>
        <div className="admin-form-grid">
          <Field label="角色 id" hint="逗号或空格分隔小写给多个" htmlFor="fill-ids">
            <TextInput
              id="fill-ids"
              value={fillPersonaIds}
              onChange={setFillPersonaIds}
              placeholder="p_abc123, p_def456"
            />
          </Field>
          <Field label="每个角色补几张" htmlFor="fill-per">
            <TextInput
              id="fill-per"
              value={fillPerPersona}
              onChange={setFillPerPersona}
              placeholder="1"
            />
          </Field>
        </div>
        <div className="admin-row" style={{ marginTop: 12 }}>
          <Button onClick={() => void runFill()} loading={filling}>
            开始补图
          </Button>
        </div>
      </div>

      {/* 清理 --------------------------------------------------------------- */}
      <div className="admin-card">
        <div className="admin-card-head">
          <h2 className="admin-card-title">清理</h2>
          <span className="admin-card-sub">只删未引用且已过期的图</span>
          <div style={{ marginLeft: "auto" }}>
            <Button variant="danger" size="sm" onClick={() => setConfirmPrune(true)}>
              清理图库
            </Button>
          </div>
        </div>
        {pruneResult ? (
          <pre className="admin-mono" style={{ margin: 0, whiteSpace: "pre-wrap" }}>
            {JSON.stringify(pruneResult, null, 2)}
          </pre>
        ) : (
          <p className="admin-muted" style={{ margin: 0 }}>
            建议先「试算」，看清楚会删多少再真删。
          </p>
        )}
      </div>

      {/* 图库 --------------------------------------------------------------- */}
      <div className="admin-section-head" style={{ marginTop: 8 }}>
        <h2 className="admin-card-title">图库</h2>
        <div className="admin-section-actions">
          <label className="admin-row" style={{ fontSize: 12 }}>
            <input
              type="checkbox"
              checked={onlyUnused}
              onChange={(event) => {
                setOnlyUnused(event.target.checked);
                setOffset(0);
              }}
            />
            <span className="admin-muted">只看未被引用的</span>
          </label>
        </div>
      </div>

      {libraryQuery.isError ? (
        <ErrorState
          title="读取图库失败"
          message={
            libraryQuery.error instanceof AdminApiError
              ? libraryQuery.error.detail
              : String(libraryQuery.error)
          }
          onRetry={() => void libraryQuery.refetch()}
        />
      ) : libraryQuery.isPending ? (
        <LoadingState />
      ) : !library?.ok ? (
        <ErrorState
          title="图库不可用"
          issues={library?.issues}
          onRetry={() => void libraryQuery.refetch()}
        />
      ) : (
        <>
          <Table<LibraryItem>
            rows={library.items}
            rowKey={(row) => row.path}
            empty={<div className="admin-table-empty">图库还是空的。用上面的「搜图入库」加图。</div>}
            columns={[
              {
                key: "thumb",
                header: "预览",
                render: (row) => (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={row.url}
                    alt={row.title}
                    style={{ width: 48, height: 48, objectFit: "cover", borderRadius: 6 }}
                    loading="lazy"
                  />
                ),
              },
              { key: "title", header: "标题", render: (row) => row.title || row.path },
              {
                key: "ref",
                header: "引用",
                render: (row) =>
                  row.referenced ? (
                    <Badge tone="info">
                      已引用{row.persona_nickname ? ` · ${row.persona_nickname}` : ""}
                    </Badge>
                  ) : (
                    <Badge>未引用</Badge>
                  ),
              },
              { key: "source", header: "来源", render: (row) => row.source || null },
              { key: "license", header: "版权", render: (row) => row.license || null },
              { key: "author", header: "作者", render: (row) => row.author || null },
              { key: "at", header: "入库时间", render: (row) => row.fetched_at || null },
            ]}
          />
          <Pager
            total={library.total}
            offset={offset}
            limit={PAGE_SIZE}
            onChange={(next) => setOffset(next)}
          />
        </>
      )}

      <ConfirmDialog
        open={confirmPrune}
        title={pruneDryRun ? "试算清理" : "确认清理图库？"}
        body={
          <div className="admin-col">
            <label className="admin-row">
              <input
                type="checkbox"
                checked={pruneDryRun}
                onChange={(event) => setPruneDryRun(event.target.checked)}
              />
              <span>只试算，不真的删（强烈建议先这样）</span>
            </label>
            <p style={{ margin: 0 }}>
              {pruneDryRun
                ? "会算一遍「如果现在清理，会删掉多少张、释放多少空间」，不会动任何文件。"
                : "会永久删除「未被任何动态引用且已过期」的图片。被引用的图不会动。"}
            </p>
          </div>
        }
        confirmLabel={pruneDryRun ? "开始试算" : "确认删除"}
        danger={!pruneDryRun}
        busy={pruning}
        onConfirm={() => void runPrune()}
        onCancel={() => setConfirmPrune(false)}
      />
    </section>
  );
}
