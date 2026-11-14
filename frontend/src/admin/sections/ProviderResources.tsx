"use client";

import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { apiDelete, apiGet, apiPatch, apiPost, apiPut, withQuery } from "../api";
import {
  buildResourcePayload, defaultResourceDraft, errorMessage, LIMIT_FIELDS,
  normalizePoolStats, PROVIDER_LABELS, PROVIDER_RESOURCES_PATH, PROVIDERS,
  resourceDraft, saveAndVerifyResource, validateResourceDraft, liveResource,
  type ProviderModes, type ProviderResource, type ResourceDraft, type ResourceGroup,
  type ResourcePage, type ResourceProvider,
} from "../providerResources";
import {
  Badge, Button, ConfirmDialog, Drawer, EmptyState, ErrorState, Field,
  LoadingState, Pager, Select, Table, TextInput, ToastStack, useToasts,
} from "../ui";

const ROOT_KEY = ["admin", "provider-resources"] as const;
const PAGE_SIZE = 25;
const STATE_LABELS: Record<string, string> = {
  pending: "待验证", active: "可用", paused: "已暂停", disabled: "已暂停",
  draining: "排空中", deleted: "已删除", cooldown: "冷却中", exhausted: "配额已耗尽", failed: "验证失败",
};
function stateLabel(resource: ProviderResource): string {
  return STATE_LABELS[resource.state] ?? resource.state;
}
function display(value: unknown): string {
  return typeof value === "number" || typeof value === "string" ? String(value) : "—";
}
function cap(value: number | undefined): string {
  return value ? String(value) : "不限";
}
function usePageVisible(): boolean {
  const [visible, setVisible] = useState(true);
  useEffect(() => {
    const update = () => setVisible(document.visibilityState === "visible");
    update();
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  return visible;
}

function LimitInputs({ draft, onChange, advanced = false }: {
  draft: Pick<ResourceDraft, "concurrency_limit" | "rpm_limit" | "tpm_limit" | "daily_limit" | "monthly_limit">;
  onChange: (key: keyof typeof draft, value: string) => void;
  advanced?: boolean;
}) {
  return <div className="admin-form-grid">{LIMIT_FIELDS.filter((_, index) => advanced ? index > 0 : index === 0).map(({ key, label, hint }) => (
    <Field key={key} htmlFor={`pool-${key}`} label={label} hint={hint}>
      <TextInput id={`pool-${key}`} type="number" value={draft[key]} onChange={(value) => onChange(key, value)} />
    </Field>
  ))}</div>;
}

function ResourceEditor({ provider, initial, defaults, quotaGroups, routeGroups, onSaved, onClose }: {
  provider: ResourceProvider;
  initial?: ProviderResource;
  defaults?: Partial<ResourceDraft>;
  quotaGroups: ResourceGroup[];
  routeGroups: ResourceGroup[];
  onSaved: (message: string) => void;
  onClose: () => void;
}) {
  const [savedResource, setSavedResource] = useState(initial);
  const [draft, setDraft] = useState(() => initial ? resourceDraft(initial) : defaultResourceDraft(provider, defaults));
  const [saving, setSaving] = useState(false);
  const [issues, setIssues] = useState<string[]>([]);
  const [savedError, setSavedError] = useState("");
  const change = (key: keyof ResourceDraft, value: string) => setDraft((current) => ({ ...current, [key]: value }));
  const selectedQuota = quotaGroups.find((group) => group.id === draft.quota_group_id);
  const save = async () => {
    const errors = validateResourceDraft(draft, savedResource);
    setIssues(errors);
    setSavedError("");
    if (errors.length) return;
    setSaving(true);
    try {
      const result = await saveAndVerifyResource(buildResourcePayload(draft, savedResource), savedResource, () => change("api_key", ""));
      setSavedResource(result.resource);
      setDraft(resourceDraft(result.resource));
      onSaved(result.ok ? result.message || "资源已通过验证" : "资源已保存，仍待验证");
      if (result.ok) onClose();
      else setSavedError(result.message || result.resource.last_error || "业务探测未通过，请修正后再次验证。");
    } catch (error) {
      setIssues([errorMessage(error)]);
    } finally {
      change("api_key", "");
      setSaving(false);
    }
  };
  const eligibleRoutes = routeGroups.filter((group) => group.provider === provider && (!group.model || group.model === draft.model.trim()));
  return (
    <Drawer open title={`${initial ? "编辑" : "添加"}${PROVIDER_LABELS[provider]}资源`} onClose={() => { if (!saving) onClose(); }}>
      <form onSubmit={(event) => { event.preventDefault(); void save(); }}>
        <fieldset disabled={saving} style={{ border: 0, padding: 0, margin: 0 }}>
          <p className="admin-muted">保存后会发起一次小型真实业务探测，可能消耗少量供应商额度。验证通过后资源自动可用；失败时保留为待验证。</p>
          {savedResource ? <p className="admin-muted">当前指纹：<span className="admin-mono">{savedResource.masked_key || savedResource.fingerprint || "—"}</span> · 修订 {savedResource.revision}</p> : null}
          <div className="admin-form-grid">
            <Field label="资源名称" htmlFor="pool-name"><TextInput id="pool-name" value={draft.name} onChange={(value) => change("name", value)} placeholder="例如：生产账号 A" /></Field>
            <Field label="API Key" htmlFor="pool-key" hint={savedResource ? "留空保留现有密钥；提交后输入框会清空" : "仅本次提交，之后只展示指纹"}><TextInput id="pool-key" type="password" value={draft.api_key} onChange={(value) => change("api_key", value)} /></Field>
            <Field label="Base URL" htmlFor="pool-base"><TextInput id="pool-base" value={draft.base_url} onChange={(value) => change("base_url", value)} placeholder="https://…" /></Field>
            <Field label="模型" htmlFor="pool-model" hint="仅在相同模型内分配请求，不自动更换模型"><TextInput id="pool-model" value={draft.model} onChange={(value) => { setDraft((current) => ({ ...current, model: value, route_group_id: "" })); }} /></Field>
            <Field label="配额组" htmlFor="pool-quota-group" hint="同一上游账号或同一份额度的密钥应共用一组，避免重复计算容量">
              <Select id="pool-quota-group" value={draft.quota_group_id} onChange={(value) => change("quota_group_id", value)} options={[...(!savedResource ? [{ value: "", label: "新建独立配额组" }] : []), ...quotaGroups.filter((group) => group.provider === provider).map((group) => ({ value: group.id, label: `${group.name} · 并发 ${group.concurrency_limit ?? "—"}` }))]} />
            </Field>
          </div>
          {draft.quota_group_id ? <p className="admin-muted">共享上限：并发 {selectedQuota?.concurrency_limit ?? savedResource?.concurrency_limit ?? "—"} · RPM {cap(selectedQuota?.rpm_limit)} · TPM {cap(selectedQuota?.tpm_limit)}。调整整组上限请使用“高级：配额与路由组”。</p> : <LimitInputs draft={draft} onChange={change} />}
          {provider === "fish" ? <div className="admin-form-grid" style={{ marginTop: 12 }}>
            <Field label="支持的音色 ID（可选）" htmlFor="pool-voices" hint="以逗号或换行分隔；留空交由探测确认"><TextInput id="pool-voices" value={draft.voice_ids} onChange={(value) => change("voice_ids", value)} /></Field>
            <Field label="支持的语言（可选）" htmlFor="pool-languages" hint="例如 en, zh"><TextInput id="pool-languages" value={draft.languages} onChange={(value) => change("languages", value)} /></Field>
          </div> : null}
          <details style={{ marginTop: 16 }}>
            <summary>高级：速率、预算、路由与余额</summary>
            <div className="admin-col" style={{ gap: 12, marginTop: 12 }}>
              {!draft.quota_group_id ? <LimitInputs draft={draft} onChange={change} advanced /> : null}
              <Field label="路由组" htmlFor="pool-route-group" hint="路由组只组织同一提供方、同一模型；不会增加额度">
                <Select id="pool-route-group" value={draft.route_group_id} onChange={(value) => change("route_group_id", value)} options={[{ value: "", label: "自动使用当前模型的路由组" }, ...eligibleRoutes.map((group) => ({ value: group.id, label: group.name }))]} />
              </Field>
              <Field label="余额来源" htmlFor="pool-balance-source" hint="供应商余额默认未知；手工记录不会自动充值或替代配额限制">
                <Select id="pool-balance-source" value={draft.balance_source} onChange={(value) => change("balance_source", value)} options={[{ value: "unknown", label: "未知" }, { value: "manual", label: "手工记录" }]} />
              </Field>
              {draft.balance_source === "manual" ? <Field label="手工余额（供应商计价单位）" htmlFor="pool-balance"><TextInput id="pool-balance" type="number" value={draft.balance_value} onChange={(value) => change("balance_value", value)} /></Field> : null}
            </div>
          </details>
          {savedError ? <div style={{ marginTop: 16 }}><ErrorState title="已保存，验证未通过；资源仍待验证" message={savedError} /></div> : null}
          {issues.length ? <div style={{ marginTop: 16 }}><ErrorState title="请检查资源设置" issues={issues} /></div> : null}
          <div className="admin-row" style={{ marginTop: 20 }}>
            <Button type="submit" variant="primary" loading={saving}>{savedResource ? "保存并验证改动" : "保存并验证"}</Button>
            <Button onClick={onClose} disabled={saving}>取消</Button>
          </div>
        </fieldset>
      </form>
    </Drawer>
  );
}

function GroupEditor({ kind, initial, provider, onClose, onSaved }: {
  kind: "quota" | "route";
  initial?: ResourceGroup;
  provider: ResourceProvider;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState(() => defaultResourceDraft(provider, {
    name: initial?.name ?? "", model: initial?.model ?? "",
    ...Object.fromEntries(LIMIT_FIELDS.map(({ key }) => [key, String(initial?.[key] ?? (key === "concurrency_limit" ? 1 : 0))])),
  }));
  const [busy, setBusy] = useState(false);
  const [issues, setIssues] = useState<string[]>([]);
  const change = (key: keyof ResourceDraft, value: string) => setDraft((current) => ({ ...current, [key]: value }));
  const save = async () => {
    const errors = kind === "quota"
      ? validateResourceDraft({ ...draft, api_key: "validation-only", base_url: "https://validation.invalid", model: "validation-only" })
      : [!draft.name.trim() ? "请填写组名称" : "", !draft.model.trim() ? "请填写路由组模型" : ""].filter(Boolean);
    setIssues(errors);
    if (errors.length) return;
    setBusy(true);
    try {
      const payload: Record<string, unknown> = { provider, name: draft.name.trim() };
      if (initial) { payload.id = initial.id; payload.revision = initial.revision; }
      if (kind === "quota") for (const { key } of LIMIT_FIELDS) payload[key] = Number(draft[key]);
      else payload.model = draft.model.trim();
      await apiPost(`${PROVIDER_RESOURCES_PATH}/${kind === "quota" ? "quota-groups" : "routing-groups"}`, payload);
      onSaved();
      onClose();
    } catch (error) { setIssues([errorMessage(error)]); }
    finally { setBusy(false); }
  };
  return <Drawer open title={`${initial ? "编辑" : "添加"}${kind === "quota" ? "配额" : "路由"}组 · ${PROVIDER_LABELS[provider]}`} onClose={() => { if (!busy) onClose(); }}>
    <form onSubmit={(event) => { event.preventDefault(); void save(); }}>
      <fieldset disabled={busy} style={{ border: 0, padding: 0, margin: 0 }}>
        <p className="admin-muted">{kind === "quota" ? "组内所有密钥共用以下容量与预算。修改会影响整组资源，不会为每把密钥各增加一份额度。" : "同一提供方、同一模型的资源才能加入同一路由组。路由分组不改变配额归属。"}</p>
        <Field label="组名称" htmlFor="pool-group-name"><TextInput id="pool-group-name" value={draft.name} onChange={(value) => change("name", value)} /></Field>
        {kind === "quota" ? <div className="admin-col" style={{ gap: 12, marginTop: 12 }}><LimitInputs draft={draft} onChange={change} /><LimitInputs draft={draft} onChange={change} advanced /></div> : <Field label="模型" htmlFor="pool-group-model"><TextInput id="pool-group-model" value={draft.model} disabled={Boolean(initial)} onChange={(value) => change("model", value)} /></Field>}
        {issues.length ? <ErrorState title="未保存" issues={issues} /> : null}
        <div className="admin-row" style={{ marginTop: 20 }}><Button type="submit" variant="primary" loading={busy}>保存组设置</Button><Button onClick={onClose} disabled={busy}>取消</Button></div>
      </fieldset>
    </form>
  </Drawer>;
}

export default function ProviderResources({ defaults = {}, onModesChange }: {
  defaults?: Partial<Record<ResourceProvider, Partial<ResourceDraft>>>;
  onModesChange: (modes: ProviderModes) => void;
}) {
  const queryClient = useQueryClient();
  const visible = usePageVisible();
  const { toasts, push } = useToasts();
  const [provider, setProvider] = useState<ResourceProvider | "">("fish");
  const [status, setStatus] = useState("");
  const [route, setRoute] = useState("");
  const [page, setPage] = useState(1);
  const [editor, setEditor] = useState<{ provider: ResourceProvider; resource?: ProviderResource } | null>(null);
  const [groupEditor, setGroupEditor] = useState<{ kind: "quota" | "route"; provider: ResourceProvider; group?: ResourceGroup } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ProviderResource | null>(null);
  const [modeTarget, setModeTarget] = useState<{ provider: ResourceProvider; enabled: boolean } | null>(null);
  const [busy, setBusy] = useState("");
  const [actionError, setActionError] = useState("");
  const list = useQuery({
    queryKey: [...ROOT_KEY, "list", provider, status, route, page],
    queryFn: ({ signal }) => apiGet<ResourcePage>(withQuery(PROVIDER_RESOURCES_PATH, { provider, status, route_group_id: route, page, page_size: PAGE_SIZE }), signal),
  });
  const groups = useQuery({
    queryKey: [...ROOT_KEY, "groups"],
    queryFn: async ({ signal }) => {
      const [quota, routing] = await Promise.all([
        apiGet<{ items: ResourceGroup[] }>(`${PROVIDER_RESOURCES_PATH}/quota-groups`, signal),
        apiGet<{ items: ResourceGroup[] }>(`${PROVIDER_RESOURCES_PATH}/routing-groups`, signal),
      ]);
      return { quota: quota.items, routing: routing.items };
    },
  });
  const ids = (list.data?.items ?? []).map((resource) => resource.id).join(",");
  const stats = useQuery({
    queryKey: [...ROOT_KEY, "stats", ids],
    queryFn: async ({ signal }) => normalizePoolStats(await apiGet<unknown>(withQuery(`${PROVIDER_RESOURCES_PATH}/stats`, { resource_ids: ids }), signal)),
    enabled: visible,
    refetchInterval: visible ? 5_000 : false,
    refetchIntervalInBackground: false,
  });
  const modesJson = JSON.stringify(Object.fromEntries(PROVIDERS.flatMap((key) => typeof stats.data?.providers[key]?.enabled === "boolean" ? [[key, stats.data.providers[key]?.enabled]] : [])));
  useEffect(() => { onModesChange(JSON.parse(modesJson) as ProviderModes); }, [modesJson, onModesChange]);
  useEffect(() => {
    if (list.data && page > Math.max(1, Math.ceil(list.data.total / PAGE_SIZE))) setPage(Math.max(1, Math.ceil(list.data.total / PAGE_SIZE)));
  }, [list.data, page]);
  const refresh = () => { void queryClient.invalidateQueries({ queryKey: ROOT_KEY }); };
  const filterProvider = (value: ResourceProvider | "") => { setProvider(value); setPage(1); setRoute(""); };
  const mutate = async (key: string, action: () => Promise<unknown>, success: string) => {
    setBusy(key);
    setActionError("");
    try { await action(); push(success); refresh(); return true; }
    catch (error) { setActionError(errorMessage(error)); return false; }
    finally { setBusy(""); }
  };
  const verify = (resource: ProviderResource) => mutate(resource.id, async () => {
    const result = await apiPost<{ ok: boolean; message?: string }>(`${PROVIDER_RESOURCES_PATH}/${encodeURIComponent(resource.id)}/verify`, { revision: resource.revision });
    refresh();
    if (!result.ok) throw new Error(result.message || "业务探测未通过，资源仍待验证");
  }, "业务探测通过，资源状态已更新");
  const quotas = groups.data?.quota ?? [];
  const routes = groups.data?.routing ?? [];
  const groupProvider = provider || "fish";
  return <div className="admin-col" style={{ gap: 20 }}>
    <ToastStack toasts={toasts} />
    <div className="admin-form-grid">
      {PROVIDERS.map((key) => {
        const runtime = stats.data?.providers[key];
        const enabled = runtime?.enabled;
        return <div className="admin-card" key={key}>
          <div className="admin-card-head"><h2 className="admin-card-title">{PROVIDER_LABELS[key]}</h2><Badge tone={enabled === true ? "success" : undefined}>{enabled === true ? "资源池已启用" : enabled === false ? "旧配置模式" : "读取模式中"}</Badge></div>
          <p className="admin-muted">使用中 {display(runtime?.active ?? runtime?.in_flight ?? runtime?.inflight)} · 等待 {display(runtime?.waiting ?? runtime?.queued)}</p>
          <div className="admin-row">
            <Button variant="primary" onClick={() => setEditor({ provider: key })} disabled={groups.isPending || groups.isError}>＋ 添加</Button>
            <Button onClick={() => filterProvider(key)}>查看资源</Button>
            <Button size="sm" disabled={enabled === undefined || Boolean(busy)} onClick={() => { setActionError(""); setModeTarget({ provider: key, enabled: !enabled }); }}>{enabled === true ? "回退旧配置" : "启用资源池"}</Button>
          </div>
        </div>;
      })}
    </div>
    <div className="admin-card">
      <div className="admin-card-head"><h2 className="admin-card-title">密钥资源</h2><Button onClick={refresh} disabled={list.isFetching}>刷新</Button></div>
      <p className="admin-muted">资源只显示脱敏指纹。已验证资源才可启用；暂停或删除后停止接收新请求，正在执行的请求会先排空。统计每 5 秒更新当前页，切到后台后暂停。</p>
      <div className="admin-form-grid" style={{ marginBottom: 16 }}>
        <Field label="提供方" htmlFor="pool-provider-filter"><Select id="pool-provider-filter" value={provider} onChange={filterProvider} options={[{ value: "", label: "全部提供方" }, ...PROVIDERS.map((key) => ({ value: key, label: PROVIDER_LABELS[key] }))]} /></Field>
        <Field label="状态" htmlFor="pool-status-filter"><Select id="pool-status-filter" value={status} onChange={(value) => { setStatus(value); setPage(1); }} options={[{ value: "", label: "全部状态" }, ...["active", "pending", "disabled", "cooldown", "exhausted", "draining"].map((key) => ({ value: key, label: STATE_LABELS[key] }))]} /></Field>
        <Field label="路由组" htmlFor="pool-route-filter"><Select id="pool-route-filter" value={route} onChange={(value) => { setRoute(value); setPage(1); }} options={[{ value: "", label: "全部路由组" }, ...routes.filter((group) => !provider || group.provider === provider).map((group) => ({ value: group.id, label: `${group.name}${group.model ? ` · ${group.model}` : ""}` }))]} /></Field>
      </div>
      {actionError ? <ErrorState title="操作未完成" message={actionError} /> : null}
      {stats.isError ? <ErrorState title="实时统计暂不可用" message={errorMessage(stats.error)} onRetry={() => void stats.refetch()} /> : null}
      {list.isError ? <ErrorState title="读取资源失败" message={errorMessage(list.error)} onRetry={() => void list.refetch()} /> : <>
        <Table rows={list.data?.items ?? []} rowKey={(resource) => resource.id} loading={list.isPending} empty={<EmptyState title="还没有符合条件的资源" description="选择提供方后点击“添加”，保存并验证后即可使用。" />} columns={[
          { key: "resource", header: "资源 / 指纹", render: (resource) => <><strong>{resource.name}</strong><div className="admin-muted">{PROVIDER_LABELS[resource.provider]}</div><div className="admin-mono">{resource.masked_key || resource.fingerprint || "—"}</div></> },
          { key: "route", header: "模型 / 端点", render: (resource) => <><div>{resource.model}</div><div className="admin-muted" style={{ overflowWrap: "anywhere", maxWidth: 240 }}>{resource.base_url}</div><div className="admin-muted">{routes.find((group) => group.id === resource.route_group_id)?.name ?? "路由组"} · 修订 {resource.revision}</div></> },
          { key: "state", header: "状态", render: (resource) => {
            const live = liveResource(resource, stats.data?.resources[resource.id] ?? resource.runtime);
            return <><Badge tone={live.state === "active" && live.enabled ? "success" : live.state === "pending" ? "warn" : undefined}>{stateLabel(live)}</Badge>{!live.enabled && live.state !== "paused" ? <div className="admin-muted">已停用</div> : null}{live.last_error ? <div className="admin-field-error" style={{ maxWidth: 240, overflowWrap: "anywhere" }}>{live.last_error}</div> : null}</>;
          } },
          { key: "quota", header: "共享配额", render: (resource) => <><div>{quotas.find((group) => group.id === resource.quota_group_id)?.name ?? "独立配额组"}</div><div className="admin-muted">并发 {resource.concurrency_limit ?? "—"} · RPM {cap(resource.rpm_limit)} · TPM {cap(resource.tpm_limit)}</div><div className="admin-muted">日 {cap(resource.daily_limit)} / 月 {cap(resource.monthly_limit)}</div><div className="admin-muted">余额 {resource.balance_source === "manual" ? `${resource.balance_value ?? "—"}（手工）` : "未知"}</div></> },
          { key: "runtime", header: "运行情况", render: (resource) => {
            const runtime = stats.data?.resources[resource.id] ?? resource.runtime;
            return <><div>使用中 {display(runtime?.active ?? runtime?.in_flight ?? runtime?.inflight)}</div><div className="admin-muted">冷却 {display(runtime?.cooldown_seconds ?? runtime?.cooldown_remaining ?? runtime?.cooldown_s)} 秒</div><div className="admin-muted">耗时均值 {display(runtime?.ewma_seconds)} 秒</div><div className="admin-muted">完成 {display(runtime?.completed)} · 失败 {display(runtime?.failures)}</div><div className="admin-muted">组今日请求 {display(runtime?.daily_requests)} · 本月 {display(runtime?.monthly_requests)}</div></>;
          } },
          { key: "actions", header: "操作", render: (resource) => <div className="admin-row" style={{ minWidth: 170 }}>
            <Button size="sm" disabled={Boolean(busy)} onClick={() => setEditor({ provider: resource.provider, resource })}>编辑</Button>
            <Button size="sm" loading={busy === resource.id} disabled={Boolean(busy)} onClick={() => void verify(resource)}>测试 / 验证</Button>
            <Button size="sm" disabled={Boolean(busy) || (!resource.enabled && (!resource.verified_at || resource.state === "pending"))} title={!resource.enabled && (!resource.verified_at || resource.state === "pending") ? "请先通过验证" : undefined} onClick={() => void mutate(resource.id, () => apiPatch(`${PROVIDER_RESOURCES_PATH}/${encodeURIComponent(resource.id)}`, { enabled: !resource.enabled, revision: resource.revision }), resource.enabled ? "资源已暂停，执行中的请求将排空" : "资源已启用")}>{resource.enabled ? "暂停" : "启用"}</Button>
            <Button size="sm" variant="danger" disabled={Boolean(busy)} onClick={() => { setActionError(""); setDeleteTarget(resource); }}>删除</Button>
          </div> },
        ]} />
        <Pager total={list.data?.total ?? 0} offset={(page - 1) * PAGE_SIZE} limit={PAGE_SIZE} onChange={(offset) => setPage(Math.floor(offset / PAGE_SIZE) + 1)} />
      </>}
    </div>
    <details className="admin-card">
      <summary>高级：配额与路由组</summary>
      <p className="admin-muted">配额组决定共享的并发、速率和预算；路由组组织同模型资源。重复添加同一密钥不会产生额外额度。</p>
      {groups.isPending ? <LoadingState label="正在读取资源组…" /> : groups.isError ? <ErrorState title="读取资源组失败" message={errorMessage(groups.error)} onRetry={() => void groups.refetch()} /> : <>
        <div className="admin-row" style={{ marginBottom: 12 }}>
          <Button onClick={() => setGroupEditor({ kind: "quota", provider: groupProvider })}>＋ {PROVIDER_LABELS[groupProvider]}配额组</Button>
          <Button onClick={() => setGroupEditor({ kind: "route", provider: groupProvider })}>＋ {PROVIDER_LABELS[groupProvider]}路由组</Button>
        </div>
        <Table rows={quotas.filter((group) => !provider || group.provider === provider)} rowKey={(group) => group.id} columns={[
          { key: "name", header: "配额组", render: (group) => <>{group.name}<div className="admin-muted">{PROVIDER_LABELS[group.provider]}</div></> },
          { key: "limits", header: "整组上限（共享）", render: (group) => `并发 ${group.concurrency_limit ?? "—"} · RPM ${cap(group.rpm_limit)} · TPM ${cap(group.tpm_limit)} · 日 ${cap(group.daily_limit)} / 月 ${cap(group.monthly_limit)}` },
          { key: "edit", header: "操作", render: (group) => <Button size="sm" onClick={() => setGroupEditor({ kind: "quota", provider: group.provider, group })}>编辑整组限额</Button> },
        ]} />
        <div style={{ marginTop: 12 }}><Table rows={routes.filter((group) => !provider || group.provider === provider)} rowKey={(group) => group.id} columns={[
          { key: "name", header: "路由组", render: (group) => group.name },
          { key: "model", header: "提供方 / 模型", render: (group) => `${PROVIDER_LABELS[group.provider]} · ${group.model ?? "—"}` },
          { key: "edit", header: "操作", render: (group) => <Button size="sm" onClick={() => setGroupEditor({ kind: "route", provider: group.provider, group })}>编辑路由组</Button> },
        ]} /></div>
      </>}
    </details>
    {groups.isError ? <ErrorState title="资源组暂不可用，添加功能已暂停" message={errorMessage(groups.error)} onRetry={() => void groups.refetch()} /> : null}
    {editor ? <ResourceEditor key={editor.resource?.id ?? editor.provider} provider={editor.provider} initial={editor.resource} defaults={defaults[editor.provider]} quotaGroups={quotas} routeGroups={routes} onClose={() => setEditor(null)} onSaved={(message) => { push(message); refresh(); }} /> : null}
    {groupEditor ? <GroupEditor kind={groupEditor.kind} provider={groupEditor.provider} initial={groupEditor.group} onClose={() => setGroupEditor(null)} onSaved={() => { push("资源组已保存"); refresh(); }} /> : null}
    <ConfirmDialog open={Boolean(deleteTarget)} title="删除密钥资源？" danger busy={busy === "delete"} confirmLabel="删除并排空" onCancel={() => { if (!busy) setDeleteTarget(null); }} onConfirm={() => { if (deleteTarget) void mutate("delete", () => apiDelete(`${PROVIDER_RESOURCES_PATH}/${encodeURIComponent(deleteTarget.id)}`), "资源已删除，执行中的请求将先排空").then((ok) => { if (ok) setDeleteTarget(null); }); }} body={<><p>删除“{deleteTarget?.name}”后将停止向它分配新请求。正在执行的请求会先完成，再释放旧密钥连接。此操作不能撤销。</p>{actionError ? <ErrorState title="删除未完成" message={actionError} /> : null}</>} />
    <ConfirmDialog open={Boolean(modeTarget)} title={modeTarget?.enabled ? "启用供应商资源池？" : "回退到旧配置？"} busy={busy === "mode"} confirmLabel={modeTarget?.enabled ? "启用资源池" : "确认回退"} onCancel={() => { if (!busy) setModeTarget(null); }} onConfirm={() => { if (modeTarget) void mutate("mode", () => apiPut(`${PROVIDER_RESOURCES_PATH}/${modeTarget.provider}/mode`, { enabled: modeTarget.enabled }), modeTarget.enabled ? "资源池模式已启用" : "已明确切回旧配置模式").then((ok) => { if (ok) setModeTarget(null); }); }} body={<><p>{PROVIDER_LABELS[modeTarget?.provider ?? "fish"]}：{modeTarget?.enabled ? "已配置过的模型将按资源池的可用性和配额调度；资源不足时不会绕过配额偷偷使用旧密钥。" : "后续请求将使用下方保留的旧配置。资源不会被删除；以后需要手工重新启用资源池。"}</p>{actionError ? <ErrorState title="模式未切换" message={actionError} /> : null}</>} />
  </div>;
}
