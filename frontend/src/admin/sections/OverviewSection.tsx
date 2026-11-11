"use client";

/**
 * 总览：服务健康、今日产出、后台任务。
 *
 * 数据来自 `GET /api/admin/overview`（一个聚合接口，避免首屏打 8 个请求）。
 */

import { useQuery } from "@tanstack/react-query";

import { apiGet, AdminApiError } from "../api";
import { adminKeys } from "../queryKeys";
import { Badge, ErrorState, LoadingState } from "../ui";

interface ServiceStatus {
  configured: boolean;
  ok: boolean;
  source?: string;
  detail?: string;
}

interface OverviewPayload {
  users: { total: number; real: number; empty: number; registered: number; active_today: number };
  personas: { total: number; active: number };
  today: { events: number; posts_pending: number; posts_published: number };
  services: Record<string, ServiceStatus>;
  jobs: { failed_recent: Array<Record<string, unknown>>; running: number; queued: number };
  push: { pending: number; sent: number; failed: number; unconfigured: number };
  server_time?: number;
}

const SERVICE_LABEL: Record<string, string> = {
  llm: "LLM",
  stt: "语音识别 STT",
  tts: "语音合成 TTS",
  deepgram: "Deepgram 流式识别",
  jpush: "JPush 推送",
};

function Stat({ label, value, hint }: { label: string; value: number | string; hint?: string }) {
  return (
    <div className="admin-stat">
      <div className="admin-stat-label">{label}</div>
      <div className="admin-stat-value">{value}</div>
      {hint ? <div className="admin-stat-hint">{hint}</div> : null}
    </div>
  );
}

function ServiceRow({ name, status }: { name: string; status: ServiceStatus }) {
  const label = SERVICE_LABEL[name] || name;
  if (!status.configured) {
    return (
      <tr>
        <td>{label}</td>
        <td>
          <Badge>未配置</Badge>
        </td>
        <td className="admin-muted">{status.source === "env" ? "环境变量" : "后台"}</td>
        <td className="admin-muted">{status.detail || "—"}</td>
      </tr>
    );
  }
  return (
    <tr>
      <td>{label}</td>
      <td>
        {status.ok ? (
          <Badge tone="success">可用</Badge>
        ) : (
          <Badge tone="error">异常</Badge>
        )}
      </td>
      <td className="admin-muted">{status.source === "env" ? "环境变量" : "后台"}</td>
      <td className="admin-muted">{status.detail || "—"}</td>
    </tr>
  );
}

export default function OverviewSection() {
  const query = useQuery({
    queryKey: adminKeys.overview(),
    queryFn: () => apiGet<OverviewPayload>("/api/admin/overview"),
  });

  if (query.isPending) return <LoadingState label="正在读取总览…" />;
  if (query.isError) {
    const error = query.error;
    return (
      <ErrorState
        title="读取总览失败"
        message={error instanceof AdminApiError ? error.detail : String(error)}
        issues={error instanceof AdminApiError ? error.issues : undefined}
        onRetry={() => void query.refetch()}
      />
    );
  }

  const data = query.data;
  const failed = data.jobs.failed_recent || [];

  return (
    <section className="admin-section">
      <div className="admin-section-head">
        <div>
          <h1 className="admin-section-title">总览</h1>
          <p className="admin-section-desc">
            服务健康、今日产出与后台任务。数据每 30 秒自动刷新。
          </p>
        </div>
        <div className="admin-section-actions">
          <button
            type="button"
            className="admin-btn"
            onClick={() => void query.refetch()}
            disabled={query.isFetching}
          >
            {query.isFetching ? "刷新中…" : "刷新"}
          </button>
        </div>
      </div>

      <div className="admin-stat-grid">
        {/* 「用户」这一格必须诚实。v3_users 的行数 != 人数：客户端在空白
            localStorage 里启动（无痕窗口 / 重装 / 清数据）就会新造一个身份。
            所以主数字用"有使用痕迹的"，原始行数降级为副标题。 */}
        <Stat
          label="真实用户"
          value={data.users.real}
          hint={
            data.users.empty > 0
              ? `共 ${data.users.total} 条注册记录，${data.users.empty} 条无使用痕迹`
              : `共 ${data.users.total} 条注册记录`
          }
        />
        <Stat
          label="注册设备"
          value={data.users.registered}
          hint="装过 App 并授权通知"
        />
        <Stat label="今日活跃" value={data.users.active_today} />
        <Stat label="AI 角色" value={data.personas.total} hint={`其中 ${data.personas.active} 个在用`} />
        <Stat label="今日事件" value={data.today.events} />
        <Stat label="今日已发动态" value={data.today.posts_published} />
        <Stat label="待发动态" value={data.today.posts_pending} hint="队列中的动态" />
      </div>

      <div className="admin-grid-2">
        <div className="admin-card">
          <div className="admin-card-head">
            <h2 className="admin-card-title">服务状态</h2>
          </div>
          <div className="admin-table-wrap" style={{ border: 0 }}>
            <table className="admin-table">
              <thead>
                <tr>
                  <th>服务</th>
                  <th>状态</th>
                  <th>来源</th>
                  <th>说明</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(data.services || {}).map(([name, status]) => (
                  <ServiceRow key={name} name={name} status={status} />
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="admin-card">
          <div className="admin-card-head">
            <h2 className="admin-card-title">后台任务</h2>
            <span className="admin-card-sub">
              运行中 {data.jobs.running} · 排队 {data.jobs.queued}
            </span>
          </div>
          {failed.length === 0 ? (
            <p className="admin-muted" style={{ margin: 0 }}>
              最近没有失败的任务。
            </p>
          ) : (
            <div className="admin-table-wrap" style={{ border: 0 }}>
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>类型</th>
                    <th>错误</th>
                  </tr>
                </thead>
                <tbody>
                  {failed.map((job, index) => (
                    <tr key={String(job.id ?? index)}>
                      <td>{String(job.kind ?? "")}</td>
                      <td className="admin-mono">{String(job.last_error ?? "")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      <div className="admin-card">
        <div className="admin-card-head">
          <h2 className="admin-card-title">推送送达</h2>
        </div>
        <div className="admin-stat-grid">
          <Stat label="待发送" value={data.push.pending} />
          <Stat label="已送达" value={data.push.sent} />
          <Stat label="失败" value={data.push.failed} />
          <Stat label="未配置" value={data.push.unconfigured} />
        </div>
      </div>
    </section>
  );
}
