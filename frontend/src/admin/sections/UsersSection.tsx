"use client";

/**
 * 用户管理（`#/users`）：用户列表 + 搜索 + 排序 + 分页。
 *
 * 用的接口
 * --------
 * `GET /api/admin/users?search=&offset=&limit=&sort=`
 *   → `{ok, total, offset, limit, items:[{id, display_name, device_key_tail,
 *        created_at, last_seen_at, counts:{posts,friends,devices}}]}`
 *
 * 为什么这是一个"纯列表"分区
 * --------------------------
 * 用户详情（设备 / 推送 / 好友 / 指派好友）在 `UserDetailSection`。列表只负责
 * "找到人"，所以渠道只有两个：搜索（昵称 / 设备尾号）与排序。
 * 点任意一行 → `#/users/{uid}`。
 *
 * 产品决定（不是漏做）
 * --------------------
 * 管理台**不提供聊天记录查看**。列表页只展示账号元数据（昵称、设备尾号、
 * 动态 / 好友 / 设备计数、最近活跃），不展示任何对话内容。
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { AdminApiError, apiGet, withQuery } from "../api";
import { adminKeys } from "../queryKeys";
import { adminRouteToHash } from "../router";
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  LoadingState,
  Pager,
  Select,
  Table,
  TextInput,
} from "../ui";

/* -------------------------------------------------------------------------- */
/* 后端数据形状                                                                */
/* -------------------------------------------------------------------------- */

interface UserCounts {
  posts?: number;
  friends?: number;
  devices?: number;
}

interface UserRow {
  id: string;
  display_name?: string;
  /** 设备 key 尾号（后端已脱敏，只给尾部几位）。 */
  device_key_tail?: string;
  /** 旧投影 `persona_studio.list_users` 用的字段名；后端若沿用则一样能显示。 */
  device_key_masked?: string;
  created_at?: string;
  last_seen_at?: string;
  /** 是否有任何使用痕迹（设备/好友/动态/消息）。由后端判定，前端不自己推。 */
  has_trace?: boolean;
  counts?: UserCounts;
}

/** 后端返回的口径汇总（不受 `scope` 过滤影响）。 */
interface UsersSummary {
  total?: number;
  real?: number;
  empty?: number;
  registered?: number;
}

interface UsersPayload {
  ok?: boolean;
  total?: number;
  offset?: number;
  limit?: number;
  scope?: string;
  summary?: UsersSummary;
  items?: UserRow[];
  issues?: string[];
}

type SortKey = "last_seen" | "created" | "posts";

/**
 * 口径：`real` = 有使用痕迹，`empty` = 注册后没有任何痕迹。
 *
 * 为什么需要它：客户端在空白 localStorage 里启动就会新造一个 `user_id`
 * （无痕窗口、重装 App、清应用数据），所以 `v3_users` 行数远大于人数。
 * 默认停在 `real`——运营想看的是"有多少人在用"，不是"有多少条注册记录"。
 */
type ScopeKey = "all" | "real" | "empty";

const PAGE_SIZE = 20;

const SORT_OPTIONS: Array<{ value: SortKey; label: string }> = [
  { value: "last_seen", label: "最近活跃" },
  { value: "created", label: "创建时间" },
  { value: "posts", label: "动态数" },
];

const SCOPE_OPTIONS: Array<{ value: ScopeKey; label: string }> = [
  { value: "real", label: "有使用痕迹" },
  { value: "empty", label: "仅注册未使用" },
  { value: "all", label: "全部" },
];

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

function errorMessage(error: unknown): string {
  if (error instanceof AdminApiError) {
    return error.issues.join("；") || error.detail || error.message;
  }
  if (error instanceof Error) return error.message;
  return String(error);
}

/**
 * 时间格式化：后端不同表的同一语义字段分别用**秒级 epoch**（`jobs` 等）
 * 和 **ISO 文本**（`v3_users.created_at`）两种存法，这里统一吃掉。
 */
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

/** 设备尾号：优先用契约字段，回落到旧投影的脱敏字段。 */
function deviceTail(row: UserRow): string {
  const tail = asText(row.device_key_tail) || asText(row.device_key_masked);
  return tail;
}

function displayName(row: UserRow): string {
  return asText(row.display_name) || "（未命名）";
}

function goDetail(userId: string): void {
  window.location.hash = adminRouteToHash({ name: "userDetail", userId });
}

/* -------------------------------------------------------------------------- */
/* 分区                                                                        */
/* -------------------------------------------------------------------------- */

export default function UsersSection() {
  /** 输入框里的字（未提交）；search 是真正发给后端的值。 */
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<SortKey>("last_seen");
  // 默认只看"有使用痕迹"的：空注册记录会把真正要找的人淹掉
  const [scope, setScope] = useState<ScopeKey>("real");
  const [offset, setOffset] = useState(0);

  const params = { search, offset, limit: PAGE_SIZE, sort, scope };

  const listQuery = useQuery({
    queryKey: adminKeys.users(params),
    queryFn: () =>
      apiGet<UsersPayload>(
        withQuery("/api/admin/users", {
          search,
          offset,
          limit: PAGE_SIZE,
          sort,
          scope,
        }),
      ),
  });

  const applySearch = () => {
    setOffset(0);
    setSearch(searchInput.trim());
  };

  const clearSearch = () => {
    setSearchInput("");
    setSearch("");
    setOffset(0);
  };

  const data = listQuery.data;
  const rows = data?.items ?? [];
  const summary = data?.summary;
  const total = asNumber(data?.total ?? rows.length);
  const businessError = data && data.ok === false;

  return (
    <section className="admin-section">
      <div className="admin-section-head">
        <div>
          <h1 className="admin-section-title">用户管理</h1>
          <p className="admin-section-desc">
            按昵称或设备尾号搜索；点任意一行查看该用户的设备、推送状态与好友。
            管理台不读取用户与角色的聊天记录。
          </p>
        </div>
        <div className="admin-section-actions">
          <Button onClick={() => void listQuery.refetch()} disabled={listQuery.isFetching}>
            {listQuery.isFetching ? "刷新中…" : "刷新"}
          </Button>
        </div>
      </div>

      <div className="admin-row">
        <div style={{ flex: "1 1 260px" }}>
          <TextInput
            value={searchInput}
            onChange={setSearchInput}
            placeholder="搜索昵称 / 设备尾号"
            type="search"
            onEnter={applySearch}
          />
        </div>
        <Button onClick={applySearch}>搜索</Button>
        {search ? (
          <Button variant="ghost" onClick={clearSearch}>
            清除
          </Button>
        ) : null}
        <span className="admin-muted">排序</span>
        <Select<SortKey>
          value={sort}
          onChange={(value) => {
            setSort(value);
            setOffset(0);
          }}
          options={SORT_OPTIONS}
        />
        <span className="admin-muted">范围</span>
        <Select<ScopeKey>
          value={scope}
          onChange={(value) => {
            setScope(value);
            setOffset(0);
          }}
          options={SCOPE_OPTIONS}
        />
      </div>

      {/* 口径说明：把"注册记录数 vs 真实用户数"摆到明面，避免又出现
          "为什么有 91 个用户"这种误读。数字来自后端 summary（不受 scope 影响）。 */}
      {summary && asNumber(summary.total) > 0 ? (
        <p className="admin-muted" style={{ margin: 0, fontSize: 12 }}>
          有使用痕迹 <strong>{asNumber(summary.real)}</strong> 个
          {asNumber(summary.registered) > 0
            ? `（其中 ${asNumber(summary.registered)} 个注册了推送设备）`
            : ""}
          ；仅注册未使用 <strong>{asNumber(summary.empty)}</strong> 个，合计{" "}
          {asNumber(summary.total)} 条注册记录。
          <br />
          说明：客户端在无痕窗口、重装 App 或清数据后会重新生成身份，因此「注册记录」会多于真实人数。
        </p>
      ) : null}

      {listQuery.isError ? (
        <ErrorState
          title="读取用户列表失败"
          message={errorMessage(listQuery.error)}
          issues={listQuery.error instanceof AdminApiError ? listQuery.error.issues : undefined}
          onRetry={() => void listQuery.refetch()}
        />
      ) : listQuery.isPending ? (
        <LoadingState label="正在读取用户…" />
      ) : businessError ? (
        <ErrorState title="用户列表不可用" issues={data?.issues} />
      ) : (
        <>
          <Table<UserRow>
            rows={rows}
            rowKey={(row) => row.id}
            onRowClick={(row) => goDetail(row.id)}
            empty={
              <EmptyState
                title={search ? "没有匹配的用户" : scope === "empty" ? "没有空注册记录" : "还没有用户"}
                description={
                  search
                    ? "换个昵称关键词或设备尾号试试，或点「清除」看全部。"
                    : scope === "real"
                      ? "还没有任何用户产生过好友、动态或设备注册。想看到空注册记录，把「范围」切到「全部」。"
                      : scope === "empty"
                        ? "所有注册记录都有使用痕迹，没有可清理的空壳。"
                        : "用户首次在 App 里登录后，这里会出现账号记录。"
                }
              />
            }
            columns={[
              {
                key: "name",
                header: "昵称",
                render: (row) => (
                  <div className="admin-col" style={{ gap: 2 }}>
                    <span>{displayName(row)}</span>
                    <span className="admin-mono admin-muted">
                      {row.id}
                      {row.created_at ? ` · 创建于 ${formatTime(row.created_at)}` : ""}
                    </span>
                  </div>
                ),
              },
              {
                // 后端判定，前端只展示：避免前后端各算一遍导致"筛出来是空壳但没标出来"
                key: "trace",
                header: "使用痕迹",
                width: "96px",
                render: (row) =>
                  row.has_trace ? (
                    <Badge tone="success">有</Badge>
                  ) : (
                    <Badge tone="warn">仅注册</Badge>
                  ),
              },
              {
                key: "device",
                header: "设备尾号",
                render: (row) => (
                  <span className="admin-mono">{deviceTail(row) || null}</span>
                ),
              },
              {
                key: "posts",
                header: "动态",
                numeric: true,
                width: "72px",
                render: (row) => asNumber(row.counts?.posts),
              },
              {
                key: "friends",
                header: "好友",
                numeric: true,
                width: "72px",
                render: (row) => asNumber(row.counts?.friends),
              },
              {
                key: "devices",
                header: "设备",
                numeric: true,
                width: "72px",
                render: (row) => asNumber(row.counts?.devices),
              },
              {
                key: "last_seen",
                header: "最近活跃",
                render: (row) => formatTime(row.last_seen_at) || null,
              },
            ]}
          />
          <Pager total={total} offset={offset} limit={PAGE_SIZE} onChange={(next) => setOffset(next)} />
          <p className="admin-muted" style={{ margin: 0 }}>
            点任意一行进入详情：可以看推送设备、发测试推送、管理好友关系，并给用户指派新好友。
          </p>
        </>
      )}
    </section>
  );
}
