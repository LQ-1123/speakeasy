"use client";

/**
 * 后台任务：查看 11 种定时任务的状态，并支持「立即执行」。
 *
 * 这补上了旧版最大的运维盲区——任务失败了没有任何入口能看到。
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { AdminApiError, apiGet, apiPost, withQuery } from "../api";
import { adminKeys, } from "../queryKeys";
import { Badge, Button, ErrorState, LoadingState, Segmented, Table, useToasts, ToastStack } from "../ui";

interface JobRow {
  id: string;
  kind: string;
  status: string;
  attempts: number;
  priority: number;
  run_after: number;
  last_error: string;
  created_at: number;
  updated_at: number;
}

interface JobListPayload {
  items: JobRow[];
  total: number;
  offset: number;
  limit: number;
}

const STATUS_TONE: Record<string, "success" | "error" | "warn" | "info" | undefined> = {
  done: "success",
  failed: "error",
  retry: "warn",
  running: "info",
  queued: undefined,
};

const KIND_LABEL: Record<string, string> = {
  world: "世界初始化",
  daily_world: "每日世界",
  life_events: "生活事件",
  persona_dna: "角色 DNA",
  voice_readiness: "音色就绪",
  audio_warm: "音频预热",
  cleanup: "清理",
  event_posts: "事件动态",
  call_memory: "通话记忆",
  followups: "追问",
  followup: "跟进消息",
};

const KIND_HINT: Record<string, string> = {
  world: "首次启动时生成世界与角色，之后仅在需要时跑",
  daily_world: "每天生成当日世界内容（角色、动态）",
  life_events: "推进角色作息与生活事件",
  persona_dna: "为缺 DNA 的角色补齐表达 DNA",
  voice_readiness: "把待验证的音色逐个验证到可用",
  audio_warm: "为最新动态预合成语音",
  cleanup: "清理过期动态、音频与图库",
  event_posts: "让角色对已发生事件发动态",
  call_memory: "通话结束后写入记忆",
  followups: "安排与投递跟进消息",
  followup: "单条跟进消息投递",
};

function formatTime(value: number): string {
  if (!value) return "—";
  const date = new Date(value * 1000);
  if (Number.isNaN(date.getTime())) return "—";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

export default function JobsSection() {
  const queryClient = useQueryClient();
  const { toasts, push } = useToasts();
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [offset, setOffset] = useState(0);
  const limit = 40;

  const listQuery = useQuery({
    queryKey: adminKeys.jobs({ status: statusFilter, offset, limit }),
    queryFn: () =>
      apiGet<JobListPayload>(
        withQuery("/api/admin/jobs", { status: statusFilter, offset, limit }),
      ),
    refetchInterval: 15_000,
  });

  const kindsQuery = useQuery({
    queryKey: adminKeys.jobKinds(),
    queryFn: () => apiGet<{ kinds: string[] }>("/api/admin/jobs/kinds"),
    staleTime: 10 * 60_000,
  });

  const runMutation = useMutation({
    mutationFn: (kind: string) => apiPost<{ ok: boolean; id: string }>(`/api/admin/jobs/${kind}/run`),
    onSuccess: (_data, kind) => {
      push(`已排队执行：${KIND_LABEL[kind] || kind}`);
      void queryClient.invalidateQueries({ queryKey: ["admin", "jobs"] });
    },
    onError: (error) => {
      const message =
        error instanceof AdminApiError
          ? error.issues.join("；") || error.detail
          : String(error);
      push(message, "error");
    },
  });

  const kinds = kindsQuery.data?.kinds ?? [];

  return (
    <section className="admin-section">
      <ToastStack toasts={toasts} />
      <div className="admin-section-head">
        <div>
          <h1 className="admin-section-title">后台任务</h1>
          <p className="admin-section-desc">
            定时任务的状态与执行记录。失败的任务会保留错误信息，可手动重跑。
          </p>
        </div>
        <div className="admin-section-actions">
          <Button onClick={() => void listQuery.refetch()} disabled={listQuery.isFetching}>
            {listQuery.isFetching ? "刷新中…" : "刷新"}
          </Button>
        </div>
      </div>

      <div className="admin-card">
        <div className="admin-card-head">
          <h2 className="admin-card-title">立即执行</h2>
          <span className="admin-card-sub">排队后由后台 worker 按优先级领取</span>
        </div>
        <div className="admin-row">
          {kinds.map((kind) => (
            <Button
              key={kind}
              size="sm"
              title={KIND_HINT[kind] || kind}
              loading={runMutation.isPending && runMutation.variables === kind}
              onClick={() => runMutation.mutate(kind)}
            >
              {KIND_LABEL[kind] || kind}
            </Button>
          ))}
          {kinds.length === 0 && !kindsQuery.isPending ? (
            <span className="admin-muted">没有可执行的任务类型。</span>
          ) : null}
        </div>
      </div>

      <div className="admin-row">
        <span className="admin-muted">状态筛选</span>
        <Segmented
          value={statusFilter}
          onChange={(value) => {
            setStatusFilter(value);
            setOffset(0);
          }}
          options={[
            { value: "", label: "全部" },
            { value: "queued", label: "排队" },
            { value: "running", label: "运行中" },
            { value: "failed", label: "失败" },
            { value: "done", label: "完成" },
          ]}
        />
      </div>

      {listQuery.isError ? (
        <ErrorState
          title="读取任务列表失败"
          message={
            listQuery.error instanceof AdminApiError
              ? listQuery.error.detail
              : String(listQuery.error)
          }
          onRetry={() => void listQuery.refetch()}
        />
      ) : listQuery.isPending ? (
        <LoadingState />
      ) : (
        <>
          <Table<JobRow>
            columns={[
              {
                key: "kind",
                header: "任务类型",
                render: (row) => (
                  <span title={KIND_HINT[row.kind] || row.kind}>
                    {KIND_LABEL[row.kind] || row.kind}
                  </span>
                ),
              },
              {
                key: "status",
                header: "状态",
                render: (row) => <Badge tone={STATUS_TONE[row.status]}>{row.status}</Badge>,
              },
              { key: "attempts", header: "尝试", numeric: true, render: (row) => row.attempts },
              {
                key: "run_after",
                header: "下次运行",
                render: (row) => formatTime(row.run_after),
              },
              {
                key: "updated_at",
                header: "更新时间",
                render: (row) => formatTime(row.updated_at),
              },
              {
                key: "last_error",
                header: "上次错误",
                render: (row) =>
                  row.last_error ? (
                    <span className="admin-mono admin-truncate" style={{ maxWidth: 320, display: "block" }}>
                      {row.last_error}
                    </span>
                  ) : null,
              },
            ]}
            rows={listQuery.data.items}
            rowKey={(row) => row.id}
            empty={<div className="admin-table-empty">没有符合条件的任务记录。</div>}
          />
          <div className="admin-pager">
            <span className="admin-muted">
              共 {listQuery.data.total} 条 · 显示 {offset + 1}–
              {Math.min(offset + limit, listQuery.data.total)}
            </span>
            <Button size="sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>
              上一页
            </Button>
            <Button
              size="sm"
              disabled={offset + limit >= listQuery.data.total}
              onClick={() => setOffset(offset + limit)}
            >
              下一页
            </Button>
          </div>
        </>
      )}
    </section>
  );
}
