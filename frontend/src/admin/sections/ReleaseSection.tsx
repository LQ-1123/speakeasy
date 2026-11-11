"use client";

/**
 * 版本发布：APK 上传、强制更新、历史回滚。
 *
 * 一个必须让运营看懂的事实（写进页面）：
 * **侧载 Android 不能静默安装**——用户必须点一次确认。所谓"强制更新"只是
 * 在客户端把弹窗做成不可关闭，装不装仍然是用户点的那一下。
 *
 * 另一个事实：只改界面**不需要**发 APK，因为界面是服务器远程加载的
 * （Next 静态产物由后端吐出）。这一点能省掉大量无意义的发版。
 */

import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { AdminApiError, apiGet, apiPost, apiUpload } from "../api";
import { adminKeys } from "../queryKeys";
import {
  Badge,
  Button,
  ConfirmDialog,
  ErrorState,
  Field,
  LoadingState,
  Table,
  TextArea,
  TextInput,
  useToasts,
  ToastStack,
} from "../ui";

interface ReleaseView {
  available: boolean;
  platform?: string;
  channel?: string;
  versionCode: number;
  versionName?: string;
  apkUrl?: string;
  sha256?: string;
  size?: number;
  changelog?: string;
  force?: boolean;
  minVersionCode?: number;
  publishedAt?: string;
}

interface ReleaseStatus {
  current: ReleaseView;
  raw?: Record<string, unknown> | null;
  history: ReleaseView[];
  files: Array<{ filename: string; size: number; modified: string }>;
}

function humanSize(bytes: number): string {
  if (!bytes) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

function CurrentCard({ view }: { view: ReleaseView }) {
  if (!view.available) {
    return (
      <div className="admin-card">
        <div className="admin-card-head">
          <h2 className="admin-card-title">当前线上版本</h2>
          <Badge>未发布</Badge>
        </div>
        <p className="admin-muted" style={{ margin: 0 }}>
          还没有发布过 APK。客户端拉不到更新时会继续用内置版本。
        </p>
      </div>
    );
  }
  return (
    <div className="admin-card">
      <div className="admin-card-head">
        <h2 className="admin-card-title">当前线上版本</h2>
        <Badge tone="success">{view.versionName || `code ${view.versionCode}`}</Badge>
        {view.force ? <Badge tone="warn">强制更新</Badge> : null}
      </div>
      <div className="admin-stat-grid">
        <div className="admin-stat">
          <div className="admin-stat-label">versionCode</div>
          <div className="admin-stat-value">{view.versionCode}</div>
        </div>
        <div className="admin-stat">
          <div className="admin-stat-label">最低可运行版本</div>
          <div className="admin-stat-value">{view.minVersionCode || "—"}</div>
          <div className="admin-stat-hint">低于它会被要求必须更新</div>
        </div>
        <div className="admin-stat">
          <div className="admin-stat-label">大小</div>
          <div className="admin-stat-value" style={{ fontSize: 20 }}>
            {humanSize(view.size ?? 0)}
          </div>
        </div>
        <div className="admin-stat">
          <div className="admin-stat-label">发布时间</div>
          <div className="admin-stat-value" style={{ fontSize: 14 }}>
            {view.publishedAt || "—"}
          </div>
        </div>
      </div>
      {view.changelog ? (
        <p style={{ marginBottom: 0 }}>
          <strong>更新说明：</strong>
          {view.changelog}
        </p>
      ) : null}
      <p className="admin-mono admin-muted" style={{ marginBottom: 0, wordBreak: "break-all" }}>
        sha256: {view.sha256 || "—"}
      </p>
      {view.apkUrl ? (
        <p style={{ marginBottom: 0 }}>
          <a href={view.apkUrl} target="_blank" rel="noreferrer">
            下载链接
          </a>{" "}
          <span className="admin-muted">（客户端 /api/app/latest 也指向它）</span>
        </p>
      ) : null}
    </div>
  );
}

export default function ReleaseSection() {
  const query = useQuery({
    queryKey: adminKeys.release(),
    queryFn: () => apiGet<ReleaseStatus>("/api/admin/app-release"),
  });
  const { toasts, push } = useToasts();
  const fileRef = useRef<HTMLInputElement>(null);

  const [versionCode, setVersionCode] = useState("");
  const [versionName, setVersionName] = useState("");
  const [changelog, setChangelog] = useState("");
  const [force, setForce] = useState(false);
  const [minVersionCode, setMinVersionCode] = useState("");
  const [uploading, setUploading] = useState(false);
  const [confirmRollback, setConfirmRollback] = useState(false);
  const [rollingBack, setRollingBack] = useState(false);

  if (query.isPending) return <LoadingState label="正在读取发布状态…" />;
  if (query.isError) {
    const error = query.error;
    return (
      <ErrorState
        title="读取发布状态失败"
        message={error instanceof AdminApiError ? error.detail : String(error)}
        onRetry={() => void query.refetch()}
      />
    );
  }

  const data = query.data;

  const upload = async () => {
    const file = fileRef.current?.files?.[0];
    const code = Number(versionCode);
    if (!file) {
      push("请选择 APK 文件", "error");
      return;
    }
    if (!Number.isFinite(code) || code <= 0) {
      push("versionCode 必须是正整数", "error");
      return;
    }
    const form = new FormData();
    form.append("file", file);
    form.append("version_code", String(code));
    form.append("version_name", versionName || `v${code}`);
    if (changelog) form.append("changelog", changelog);
    form.append("force", force ? "true" : "false");
    if (minVersionCode) form.append("min_version_code", String(Number(minVersionCode) || 0));

    setUploading(true);
    try {
      const result = await apiUpload<{ ok?: boolean; issues?: string[]; message?: string }>(
        "/api/admin/app-release/upload",
        form,
      );
      if (result.ok === false) {
        push((result.issues ?? [result.message ?? "发布失败"]).join("；"), "error");
      } else {
        push("已发布新版本");
        setVersionCode("");
        setVersionName("");
        setChangelog("");
        setMinVersionCode("");
        setForce(false);
        if (fileRef.current) fileRef.current.value = "";
        void query.refetch();
      }
    } catch (cause) {
      const err = cause as AdminApiError;
      push(err.issues?.join("；") || err.detail || String(cause), "error");
    } finally {
      setUploading(false);
    }
  };

  const rollback = async () => {
    setRollingBack(true);
    try {
      const result = await apiPost<{ ok?: boolean; message?: string }>(
        "/api/admin/app-release/rollback",
      );
      if (result.ok === false) {
        push(result.message || "回滚失败", "error");
      } else {
        push("已回滚到上一版");
        void query.refetch();
      }
    } catch (cause) {
      push(cause instanceof AdminApiError ? cause.detail : String(cause), "error");
    } finally {
      setRollingBack(false);
      setConfirmRollback(false);
    }
  };

  return (
    <section className="admin-section">
      <ToastStack toasts={toasts} />
      <div className="admin-section-head">
        <div>
          <h1 className="admin-section-title">版本发布</h1>
          <p className="admin-section-desc">
            上传 APK 后客户端通过 <code>/api/app/latest</code> 拿到更新。回滚只切「当前版本」指针，不删历史包。
          </p>
        </div>
        <div className="admin-section-actions">
          <Button onClick={() => void query.refetch()} disabled={query.isFetching}>
            {query.isFetching ? "刷新中…" : "刷新"}
          </Button>
        </div>
      </div>

      <CurrentCard view={data.current} />

      <div className="admin-card">
        <div className="admin-card-head">
          <h2 className="admin-card-title">发布新版本</h2>
        </div>
        <div className="admin-form-grid">
          <Field label="APK 文件" htmlFor="apk-file">
            <input id="apk-file" ref={fileRef} type="file" accept=".apk" className="admin-input" />
          </Field>
          <Field label="versionCode" hint="整数，必须比当前版本大" htmlFor="apk-code">
            <TextInput
              id="apk-code"
              value={versionCode}
              onChange={setVersionCode}
              placeholder="例如 12"
            />
          </Field>
          <Field label="versionName" hint="展示给用户的版本号" htmlFor="apk-name">
            <TextInput
              id="apk-name"
              value={versionName}
              onChange={setVersionName}
              placeholder="例如 1.2.0"
            />
          </Field>
          <Field label="最低可运行版本 code" hint="留空 = 不强制" htmlFor="apk-min">
            <TextInput
              id="apk-min"
              value={minVersionCode}
              onChange={setMinVersionCode}
              placeholder="例如 10"
            />
          </Field>
          <Field label="强制更新" hint="只是让客户端弹窗不可关闭；安装仍需用户点一次" htmlFor="apk-force">
            <label className="admin-row" htmlFor="apk-force">
              <input
                id="apk-force"
                type="checkbox"
                checked={force}
                onChange={(event) => setForce(event.target.checked)}
              />
              <span className="admin-muted">标记为强制更新</span>
            </label>
          </Field>
        </div>
        <div style={{ marginTop: 12 }}>
          <Field label="更新说明" htmlFor="apk-changelog">
            <TextArea
              id="apk-changelog"
              value={changelog}
              onChange={setChangelog}
              rows={3}
              placeholder="这次更新改了什么（会显示在客户端更新弹窗里）"
            />
          </Field>
        </div>
        <div className="admin-row" style={{ marginTop: 12 }}>
          <Button variant="primary" onClick={() => void upload()} loading={uploading}>
            上传并发布
          </Button>
        </div>
        <p className="admin-muted" style={{ fontSize: 12, marginBottom: 0 }}>
          注意：Android 侧载<strong>不能静默安装</strong>，用户必须点一次确认。另外，
          <strong>只改界面不需要发 APK</strong>——界面是服务器远程加载的。
        </p>
      </div>

      <div className="admin-card">
        <div className="admin-card-head">
          <h2 className="admin-card-title">历史版本</h2>
          <Badge>{data.history.length} 条</Badge>
          <div style={{ marginLeft: "auto" }}>
            <Button
              variant="danger"
              size="sm"
              onClick={() => setConfirmRollback(true)}
              disabled={data.history.length === 0}
            >
              回滚到上一版
            </Button>
          </div>
        </div>
        <Table<ReleaseView>
          rows={data.history}
          rowKey={(row) => `${row.versionCode}-${row.publishedAt || ""}`}
          empty={<div className="admin-table-empty">还没有历史版本。</div>}
          columns={[
            { key: "version", header: "版本", render: (row) => row.versionName || `code ${row.versionCode}` },
            { key: "code", header: "code", numeric: true, render: (row) => row.versionCode },
            { key: "size", header: "大小", render: (row) => humanSize(row.size ?? 0) },
            { key: "force", header: "强制", render: (row) => (row.force ? <Badge tone="warn">是</Badge> : null) },
            { key: "at", header: "发布时间", render: (row) => row.publishedAt || "—" },
            { key: "changelog", header: "说明", render: (row) => row.changelog || null },
          ]}
        />
      </div>

      {data.files.length ? (
        <div className="admin-card">
          <div className="admin-card-head">
            <h2 className="admin-card-title">已上传的包</h2>
            <span className="admin-card-sub">服务器上实际存在的 APK 文件</span>
          </div>
          <Table<{ filename: string; size: number; modified: string }>
            rows={data.files}
            rowKey={(row) => row.filename}
            columns={[
              { key: "name", header: "文件", render: (row) => <span className="admin-mono">{row.filename}</span> },
              { key: "size", header: "大小", render: (row) => humanSize(row.size) },
              { key: "modified", header: "修改时间", render: (row) => row.modified },
            ]}
          />
        </div>
      ) : null}

      <ConfirmDialog
        open={confirmRollback}
        title="回滚到上一版？"
        body="会把客户端的「当前版本」指回上一个已发布的包。不会删除任何文件，可以再切回来。"
        confirmLabel="回滚"
        danger
        busy={rollingBack}
        onConfirm={() => void rollback()}
        onCancel={() => setConfirmRollback(false)}
      />
    </section>
  );
}
