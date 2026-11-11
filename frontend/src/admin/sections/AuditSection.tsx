"use client";

/**
 * 操作日志。
 *
 * 这张页面的用途只有一个：出问题时回答「最近谁改了什么」。
 * 所以设计上刻意做减法——
 * - 不做趋势图 / 统计面板：那是运营分析，不是排障；
 * - 摘要只显示后端给的文本（**只含字段名，不含值**，值里可能有密钥）；
 * - 默认按时间倒序、每页 50 条，先看最近的。
 *
 * 一个必须讲清楚的限制：这里**只有成功的写操作**。失败的操作（校验没过、
 * 500）不会进来——如果把它们也记下，误操作会把真正生效的改动淹掉。
 * 这条限制写在页面顶部，避免有人拿它当"完整审计"用。
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { AdminApiError, apiGet, withQuery } from "../api";
import { adminKeys } from "../queryKeys";
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  Field,
  LoadingState,
  Pager,
  Select,
  Table,
  TextInput,
  type Column,
} from "../ui";

const PAGE_SIZE = 50;

interface AuditRow {
  id: string;
  at: string;
  ip: string;
  action: string;
  target: string;
  summary: string;
}

interface AuditList {
  ok?: boolean;
  issues?: string[];
  items?: AuditRow[];
  total?: number;
  offset?: number;
  limit?: number;
}

interface ActionRow {
  action: string;
  count: number;
}

/** 时间列：后端存 ISO 字符串（本地时间），只做裁剪，不解析时区。 */
function timeText(value: string): string {
  const text = String(value || "");
  const match = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}:\d{2}:\d{2})/.exec(text);
  if (match) return `${match[2]}-${match[3]} ${match[4]}`;
  return text || "—";
}

/** action 的中文说明；未知的 action 原样显示（不吞数据）。 */
const ACTION_LABEL: Record<string, string> = {
  create: "新建",
  update: "修改",
  delete: "删除",
  upload: "上传",
  publish: "发布",
  cancel: "取消",
  claim: "领取",
  push: "推送",
  login: "登录",
  logout: "退出",
};

function actionLabel(action: string): string {
  return ACTION_LABEL[action] || action || "—";
}

export default function AuditSection() {
  const [offset, setOffset] = useState(0);
  const [action, setAction] = useState("");
  const [target, setTarget] = useState("");
  const [targetInput, setTargetInput] = useState("");
  const query = { offset, limit: PAGE_SIZE, action, target };

  const list = useQuery({
    queryKey: adminKeys.audit(query),
    queryFn: () => apiGet<AuditList>(withQuery("/api/admin/audit", query)),
    // 排障页面：切回来时自动刷新比手动点省事
    staleTime: 10_000,
  });

  const actions = useQuery({
    queryKey: adminKeys.audit({ facet: "actions" }),
    queryFn: () => apiGet<{ ok?: boolean; actions?: ActionRow[] }>("/api/admin/audit/actions"),
    staleTime: 30_000,
  });

  const columns: Column<AuditRow>[] = [
    {
      key: "at",
      header: "时间",
      width: "140px",
      render: (row) => <span className="admin-mono">{timeText(row.at)}</span>,
    },
    {
      key: "action",
      header: "动作",
      width: "90px",
      render: (row) => <Badge tone="info">{actionLabel(row.action)}</Badge>,
    },
    {
      key: "target",
      header: "对象",
      width: "260px",
      render: (row) => <span className="admin-mono">{row.target || "—"}</span>,
    },
    { key: "summary", header: "摘要", render: (row) => row.summary || "—" },
    {
      key: "ip",
      header: "来源",
      width: "130px",
      render: (row) => <span className="admin-mono admin-muted">{row.ip || "—"}</span>,
    },
  ];

  const rows = list.data?.items ?? [];
  const total = list.data?.total ?? 0;
  const failed = list.data?.ok === false;
  const error = list.error as AdminApiError | null;

  const applyTarget = () => {
    setOffset(0);
    setTarget(targetInput.trim());
  };

  return (
    <div className="admin-col" style={{ gap: 16 }}>
      <div className="admin-row" style={{ justifyContent: "space-between" }}>
        <div>
          <h1 className="admin-h1">操作日志</h1>
          <p className="admin-sub">最近谁改了什么。摘要只含字段名，不含内容值。</p>
        </div>
        <Button onClick={() => void list.refetch()}>刷新</Button>
      </div>

      <div className="admin-card">
        <div className="admin-row" style={{ alignItems: "flex-end", flexWrap: "wrap" }}>
          <div style={{ flex: "0 0 220px" }}>
            <Field label="动作" hint="按动作精确筛选">
              <Select
                value={action}
                onChange={(value) => {
                  setAction(value);
                  setOffset(0);
                }}
                options={[
                  { value: "", label: "全部动作" },
                  ...(actions.data?.actions ?? []).map((item) => ({
                    value: item.action,
                    label: `${actionLabel(item.action)}（${item.count}）`,
                  })),
                ]}
              />
            </Field>
          </div>
          <div style={{ flex: "1 1 260px" }}>
            <Field label="对象" hint="模糊匹配，如 personas / lexicons/dna">
              <TextInput
                value={targetInput}
                placeholder="输入对象后按回车"
                onChange={setTargetInput}
                onEnter={applyTarget}
              />
            </Field>
          </div>
          <Button onClick={applyTarget}>筛选</Button>
        </div>
        <p className="admin-muted" style={{ fontSize: 12, marginBottom: 0 }}>
          只记成功的写操作。失败或被拒绝的请求不进这张表——这里回答的是
          “哪些改动真的生效了”，不是“谁尝试过”。
        </p>
      </div>

      {list.isLoading ? (
        <LoadingState />
      ) : error ? (
        <ErrorState
          title="读取操作日志失败"
          message={error.detail || String(error)}
          issues={error.issues}
          onRetry={() => void list.refetch()}
        />
      ) : failed ? (
        <ErrorState
          title="读取操作日志失败"
          issues={list.data?.issues ?? ["未知错误"]}
          onRetry={() => void list.refetch()}
        />
      ) : rows.length === 0 ? (
        <EmptyState
          title="还没有操作记录"
          description={
            total === 0 && !action && !target
              ? "去别的页面改点东西（保存词库、编辑角色、发一条动态），回来就能看到。"
              : "当前筛选条件下没有记录，换个动作或对象试试。"
          }
        />
      ) : (
        <>
          <Table columns={columns} rows={rows} rowKey={(row) => row.id} />
          <Pager total={total} offset={offset} limit={PAGE_SIZE} onChange={setOffset} />
        </>
      )}
    </div>
  );
}
