"use client";

/**
 * 商业运营（`#/commerce/ads`）：品牌 → 活动 → 素材 → 角色分配 → 预览 / 投放记录。
 *
 * 对应后端 `/api/admin/ads/*`（`app/admin/api/ads.py` + `app/v3/ads.py`）。
 * 注意：投放本身还要求「已核验成年 + 主动开启合作推荐」的账号；当前首版免登录，
 * 这里配置的活动不会真正投给设备身份用户，等账号恢复后生效。
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { AdminApiError, apiGet, apiPost, withQuery } from "../api";
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  Field,
  LoadingState,
  Segmented,
  Select,
  Table,
  TextArea,
  TextInput,
  useToasts,
  ToastStack,
} from "../ui";

type Tab = "brands" | "campaigns" | "creatives" | "assignments" | "deliveries";

const TABS = [
  { value: "brands", label: "品牌" },
  { value: "campaigns", label: "活动" },
  { value: "creatives", label: "素材" },
  { value: "assignments", label: "角色分配" },
  { value: "deliveries", label: "投放记录" },
] as const;

interface Brand {
  id: string;
  name: string;
  industry: string;
  qualification: string;
  landing_url: string;
  status: string;
  created_at: string;
}

interface Campaign {
  id: string;
  brand_id: string;
  title: string;
  purpose: string;
  required_tags: string[];
  facts: string[];
  landing_url: string;
  channels: string[];
  valid_from: string;
  valid_until: string;
  status: string;
  version: number;
  created_at: string;
}

interface Creative {
  id: string;
  campaign_id: string;
  channel: string;
  title: string;
  body_template: string;
  status: string;
  reviewed_by: string;
  created_at: string;
}

interface Assignment {
  id: string;
  persona_id: string;
  score: number;
  status: string;
}

interface Delivery {
  id: string;
  user_id: string;
  campaign_id: string;
  channel: string;
  status: string;
  reserved_at: string;
}

interface Preview {
  copy: string;
  label: string;
  creative_approved: boolean;
  landing_url: string;
  facts: string[];
}

function errText(error: unknown): string {
  if (error instanceof AdminApiError) return error.message;
  return error instanceof Error ? error.message : String(error);
}

function statusTone(status: string): "info" | "success" | "warn" | "error" | undefined {
  switch (status) {
    case "enabled":
    case "approved":
    case "active":
      return "success";
    case "reviewed":
    case "draft":
    case "suggested":
      return "info";
    case "paused":
    case "waiting_voice":
      return "warn";
    case "ended":
    case "rejected":
    case "reported":
      return "error";
    default:
      return undefined;
  }
}

export default function AdsSection() {
  const { toasts, push } = useToasts();
  const [tab, setTab] = useState<Tab>("brands");
  const [brands, setBrands] = useState<Brand[]>([]);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [creatives, setCreatives] = useState<Creative[]>([]);
  const [assignments, setAssignments] = useState<Assignment[]>([]);
  const [deliveries, setDeliveries] = useState<Delivery[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [campaignId, setCampaignId] = useState("");

  const campaignOptions = useMemo(
    () => [{ value: "", label: "选择活动…" }, ...campaigns.map((c) => ({ value: c.id, label: `${c.title}（${c.status} v${c.version}）` }))],
    [campaigns],
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [brandRes, campaignRes] = await Promise.all([
        apiGet<{ items: Brand[] }>("/api/admin/ads/brands?limit=200"),
        apiGet<{ items: Campaign[] }>("/api/admin/ads/campaigns?limit=200"),
      ]);
      setBrands(brandRes.items ?? []);
      setCampaigns(campaignRes.items ?? []);
      setCampaignId((current) => current || campaignRes.items?.[0]?.id || "");
    } catch (err) {
      setError(errText(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const loadCreatives = useCallback(async (id: string) => {
    if (!id) {
      setCreatives([]);
      return;
    }
    try {
      const res = await apiGet<{ items: Creative[] }>(withQuery("/api/admin/ads/creatives", { campaign_id: id }));
      setCreatives(res.items ?? []);
    } catch (err) {
      push(errText(err), "error");
    }
  }, [push]);

  const loadAssignments = useCallback(async (id: string) => {
    if (!id) {
      setAssignments([]);
      return;
    }
    try {
      const res = await apiGet<{ items: Assignment[] }>(`/api/admin/ads/campaigns/${encodeURIComponent(id)}/assignments`);
      setAssignments(res.items ?? []);
    } catch (err) {
      push(errText(err), "error");
    }
  }, [push]);

  const loadDeliveries = useCallback(async (id: string) => {
    try {
      const res = await apiGet<{ items: Delivery[] }>(
        withQuery("/api/admin/ads/deliveries", { campaign_id: id || "", limit: 100 }),
      );
      setDeliveries(res.items ?? []);
    } catch (err) {
      push(errText(err), "error");
    }
  }, [push]);

  useEffect(() => {
    if (tab === "creatives") void loadCreatives(campaignId);
    if (tab === "assignments") void loadAssignments(campaignId);
    if (tab === "deliveries") void loadDeliveries(campaignId);
  }, [tab, campaignId, loadCreatives, loadAssignments, loadDeliveries]);

  const brandName = useCallback(
    (id: string) => brands.find((b) => b.id === id)?.name ?? id,
    [brands],
  );

  async function setStatus(campaign: Campaign, status: string) {
    setBusy(campaign.id + status);
    try {
      await apiPost(`/api/admin/ads/campaigns/${encodeURIComponent(campaign.id)}/status`, { status });
      push(`活动已更新为 ${status}`);
      await load();
    } catch (err) {
      push(errText(err), "error");
    } finally {
      setBusy("");
    }
  }

  async function preview(campaign: Campaign) {
    try {
      const data = await apiGet<Preview>(
        withQuery(`/api/admin/ads/campaigns/${encodeURIComponent(campaign.id)}/preview`, { channel: "chat" }),
      );
      push(`预览：${data.creative_approved ? data.copy : "（还没有审核通过的素材）"}`, data.creative_approved ? "info" : "error");
    } catch (err) {
      push(errText(err), "error");
    }
  }

  return (
    <div className="admin-section">
      <header className="admin-section-head">
        <div>
          <h1 className="admin-section-title">商业运营</h1>
          <p className="admin-section-desc">
            品牌 → 活动（草稿/审核/启用/暂停）→ 素材审核 → 角色分配 → 投放记录。活动只有「启用 + 有审核素材 +
            已确认角色」才可能投放；当前免登录版本不会真正投给用户。
          </p>
        </div>
        <Button variant="ghost" onClick={() => void load()} loading={loading}>
          刷新
        </Button>
      </header>

      <Segmented value={tab} onChange={(value) => setTab(value as Tab)} options={[...TABS]} />

      {error ? <ErrorState title="加载失败" message={error} onRetry={() => void load()} /> : null}

      {loading ? <LoadingState /> : null}

      {!loading && tab === "brands" ? (
        <BrandsPanel brands={brands} onChanged={() => void load()} push={push} />
      ) : null}

      {!loading && tab === "campaigns" ? (
        <CampaignsPanel
          brands={brands}
          campaigns={campaigns}
          brandName={brandName}
          busy={busy}
          push={push}
          onChanged={() => void load()}
          onStatus={(campaign, status) => void setStatus(campaign, status)}
          onPreview={(campaign) => void preview(campaign)}
        />
      ) : null}

      {!loading && tab === "creatives" ? (
        <CreativesPanel
          campaignId={campaignId}
          campaignOptions={campaignOptions}
          creatives={creatives}
          push={push}
          onSelect={setCampaignId}
          onChanged={() => void loadCreatives(campaignId)}
        />
      ) : null}

      {!loading && tab === "assignments" ? (
        <AssignmentsPanel
          campaignId={campaignId}
          campaignOptions={campaignOptions}
          assignments={assignments}
          push={push}
          onSelect={setCampaignId}
          onChanged={() => void loadAssignments(campaignId)}
        />
      ) : null}

      {!loading && tab === "deliveries" ? (
        <DeliveriesPanel
          campaignId={campaignId}
          campaignOptions={campaignOptions}
          deliveries={deliveries}
          onSelect={(id) => {
            setCampaignId(id);
            void loadDeliveries(id);
          }}
          onRefresh={() => void loadDeliveries(campaignId)}
        />
      ) : null}

      <ToastStack toasts={toasts} />
    </div>
  );
}

/* ------------------------------------------------------------------ 品牌 */

function BrandsPanel({
  brands,
  onChanged,
  push,
}: {
  brands: Brand[];
  onChanged: () => void;
  push: (message: string, tone?: "info" | "error") => void;
}) {
  const [name, setName] = useState("");
  const [industry, setIndustry] = useState("");
  const [qualification, setQualification] = useState("");
  const [landing, setLanding] = useState("");
  const [busy, setBusy] = useState(false);

  async function create() {
    if (!name.trim()) {
      push("品牌名称不能为空", "error");
      return;
    }
    setBusy(true);
    try {
      await apiPost("/api/admin/ads/brands", {
        name: name.trim(),
        industry: industry.trim(),
        qualification: qualification.trim(),
        landing_url: landing.trim(),
      });
      setName("");
      setIndustry("");
      setQualification("");
      setLanding("");
      push("品牌已创建");
      onChanged();
    } catch (err) {
      push(errText(err), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="admin-grid-2">
      <div className="admin-card">
        <h2 className="admin-card-title">新建品牌</h2>
        <Field label="名称">
          <TextInput value={name} onChange={setName} placeholder="例如：雀巢咖啡" />
        </Field>
        <Field label="行业">
          <TextInput value={industry} onChange={setIndustry} placeholder="food / tech / fashion…" />
        </Field>
        <Field label="资质说明" hint="审核时填写，不需要上传证件">
          <TextArea value={qualification} onChange={setQualification} rows={2} placeholder="资质编号、授权说明" />
        </Field>
        <Field label="落地页">
          <TextInput value={landing} onChange={setLanding} placeholder="https://…" />
        </Field>
        <Button variant="primary" onClick={() => void create()} loading={busy}>
          创建品牌
        </Button>
      </div>

      <div className="admin-card">
        <h2 className="admin-card-title">品牌列表（{brands.length}）</h2>
        <Table
          rows={brands}
          rowKey={(row) => row.id}
          columns={[
            { key: "name", header: "名称", render: (row) => row.name },
            { key: "industry", header: "行业", render: (row) => row.industry },
            { key: "status", header: "状态", render: (row) => <Badge tone={statusTone(row.status)}>{row.status}</Badge> },
            { key: "id", header: "ID", render: (row) => <code className="admin-code">{row.id}</code> },
          ]}
          empty={<EmptyState title="还没有品牌" description="先在左侧创建一个品牌。" />}
        />
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ 活动 */

function CampaignsPanel({
  brands,
  campaigns,
  brandName,
  busy,
  push,
  onChanged,
  onStatus,
  onPreview,
}: {
  brands: Brand[];
  campaigns: Campaign[];
  brandName: (id: string) => string;
  busy: string;
  push: (message: string, tone?: "info" | "error") => void;
  onChanged: () => void;
  onStatus: (campaign: Campaign, status: string) => void;
  onPreview: (campaign: Campaign) => void;
}) {
  const [brandId, setBrandId] = useState("");
  const [title, setTitle] = useState("");
  const [purpose, setPurpose] = useState("");
  const [tags, setTags] = useState("");
  const [facts, setFacts] = useState("");
  const [landing, setLanding] = useState("");
  const [chat, setChat] = useState(true);
  const [feed, setFeed] = useState(true);
  const [busyCreate, setBusyCreate] = useState(false);

  async function create() {
    if (!brandId) {
      push("先选择品牌", "error");
      return;
    }
    if (!title.trim()) {
      push("活动标题不能为空", "error");
      return;
    }
    setBusyCreate(true);
    try {
      await apiPost("/api/admin/ads/campaigns", {
        brand_id: brandId,
        title: title.trim(),
        purpose: purpose.trim(),
        required_tags: tags
          .split(/[,，\s]+/)
          .map((x) => x.trim())
          .filter(Boolean),
        facts: facts
          .split("\n")
          .map((x) => x.trim())
          .filter(Boolean),
        landing_url: landing.trim(),
        channels: [chat ? "chat" : "", feed ? "feed" : ""].filter(Boolean),
      });
      setTitle("");
      setPurpose("");
      setTags("");
      setFacts("");
      setLanding("");
      push("活动已创建（草稿），还需要审核 + 素材审核 + 启用");
      onChanged();
    } catch (err) {
      push(errText(err), "error");
    } finally {
      setBusyCreate(false);
    }
  }

  return (
    <div className="admin-grid-2">
      <div className="admin-card">
        <h2 className="admin-card-title">新建活动</h2>
        <Field label="品牌">
          <Select
            value={brandId}
            onChange={setBrandId}
            options={[{ value: "", label: "选择品牌…" }, ...brands.map((b) => ({ value: b.id, label: b.name }))]}
          />
        </Field>
        <Field label="标题">
          <TextInput value={title} onChange={setTitle} placeholder="例如：咖啡机合作" />
        </Field>
        <Field label="目的">
          <TextInput value={purpose} onChange={setPurpose} placeholder="新品推荐 / 复购…" />
        </Field>
        <Field label="匹配标签（逗号分隔）" hint="与用户兴趣/聊天需求匹配，如 coffee, 咖啡">
          <TextInput value={tags} onChange={setTags} placeholder="coffee" />
        </Field>
        <Field label="审核事实（每行一条）" hint="投放文案只允许使用这里的事实，不能由模型发挥">
          <TextArea value={facts} onChange={setFacts} rows={3} placeholder={"胶囊咖啡机 2 分钟出杯\n本周新品 9 折"} />
        </Field>
        <Field label="落地页">
          <TextInput value={landing} onChange={setLanding} placeholder="https://…" />
        </Field>
        <Field label="渠道">
          <div className="admin-inline">
            <label className="admin-check">
              <input type="checkbox" checked={chat} onChange={(e) => setChat(e.target.checked)} /> 聊天
            </label>
            <label className="admin-check">
              <input type="checkbox" checked={feed} onChange={(e) => setFeed(e.target.checked)} /> 动态
            </label>
          </div>
        </Field>
        <Button variant="primary" onClick={() => void create()} loading={busyCreate}>
          创建活动
        </Button>
      </div>

      <div className="admin-card">
        <h2 className="admin-card-title">活动列表（{campaigns.length}）</h2>
        <Table
          rows={campaigns}
          rowKey={(row) => row.id}
          columns={[
            { key: "title", header: "活动", render: (row) => <div>{row.title}<div className="admin-muted">{brandName(row.brand_id)}</div></div> },
            { key: "status", header: "状态", render: (row) => <Badge tone={statusTone(row.status)}>{row.status} v{row.version}</Badge> },
            { key: "channels", header: "渠道", render: (row) => (row.channels ?? []).join(" / ") },
            {
              key: "actions",
              header: "操作",
              render: (row) => (
                <div className="admin-inline">
                  {row.status === "draft" ? (
                    <Button size="sm" onClick={() => onStatus(row, "reviewed")} loading={busy === row.id + "reviewed"}>审核通过</Button>
                  ) : null}
                  {row.status === "reviewed" ? (
                    <Button size="sm" variant="primary" onClick={() => onStatus(row, "enabled")} loading={busy === row.id + "enabled"}>启用</Button>
                  ) : null}
                  {row.status === "enabled" ? (
                    <Button size="sm" onClick={() => onStatus(row, "paused")} loading={busy === row.id + "paused"}>暂停</Button>
                  ) : null}
                  {row.status === "paused" ? (
                    <Button size="sm" variant="primary" onClick={() => onStatus(row, "enabled")} loading={busy === row.id + "enabled"}>恢复</Button>
                  ) : null}
                  {row.status !== "ended" ? (
                    <Button size="sm" variant="danger" onClick={() => onStatus(row, "ended")} loading={busy === row.id + "ended"}>结束</Button>
                  ) : null}
                  <Button size="sm" variant="ghost" onClick={() => onPreview(row)}>预览</Button>
                </div>
              ),
            },
          ]}
          empty={<EmptyState title="还没有活动" description="先在左侧创建品牌和活动。" />}
        />
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ 素材 */

function CreativesPanel({
  campaignId,
  campaignOptions,
  creatives,
  push,
  onSelect,
  onChanged,
}: {
  campaignId: string;
  campaignOptions: Array<{ value: string; label: string }>;
  creatives: Creative[];
  push: (message: string, tone?: "info" | "error") => void;
  onSelect: (id: string) => void;
  onChanged: () => void;
}) {
  const [channel, setChannel] = useState("chat");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [facts, setFacts] = useState("");
  const [busy, setBusy] = useState(false);

  async function create() {
    if (!campaignId) {
      push("先选择活动", "error");
      return;
    }
    if (!body.trim()) {
      push("素材文案不能为空", "error");
      return;
    }
    setBusy(true);
    try {
      await apiPost("/api/admin/ads/creatives", {
        campaign_id: campaignId,
        channel,
        title: title.trim(),
        body_template: body.trim(),
        facts: facts.split("\n").map((x) => x.trim()).filter(Boolean),
      });
      setTitle("");
      setBody("");
      setFacts("");
      push("素材已提交（待审核）");
      onChanged();
    } catch (err) {
      push(errText(err), "error");
    } finally {
      setBusy(false);
    }
  }

  async function review(creative: Creative, approved: boolean) {
    const note = approved ? "" : window.prompt("驳回原因（必填）") ?? "";
    if (!approved && !note.trim()) return;
    try {
      await apiPost(`/api/admin/ads/creatives/${encodeURIComponent(creative.id)}/review`, { approved, note });
      push(approved ? "素材已通过" : "素材已驳回");
      onChanged();
    } catch (err) {
      push(errText(err), "error");
    }
  }

  return (
    <div className="admin-grid-2">
      <div className="admin-card">
        <h2 className="admin-card-title">新增素材</h2>
        <Field label="活动">
          <Select value={campaignId} onChange={onSelect} options={campaignOptions} />
        </Field>
        <Field label="渠道">
          <Select
            value={channel}
            onChange={setChannel}
            options={[{ value: "chat", label: "聊天" }, { value: "feed", label: "动态" }]}
          />
        </Field>
        <Field label="标题">
          <TextInput value={title} onChange={setTitle} placeholder="动态卡片标题（可选）" />
        </Field>
        <Field label="文案模板" hint="可用 {facts} 占位符；只允许审核事实，不允许模型自由发挥">
          <TextArea value={body} onChange={setBody} rows={3} placeholder="我最近在用 {facts}，你问的正好。" />
        </Field>
        <Field label="素材事实（每行一条）" hint="留空则使用活动事实">
          <TextArea value={facts} onChange={setFacts} rows={2} />
        </Field>
        <Button variant="primary" onClick={() => void create()} loading={busy}>
          提交素材
        </Button>
      </div>

      <div className="admin-card">
        <h2 className="admin-card-title">素材列表（{creatives.length}）</h2>
        <Table
          rows={creatives}
          rowKey={(row) => row.id}
          columns={[
            { key: "channel", header: "渠道", render: (row) => row.channel },
            { key: "title", header: "标题", render: (row) => row.title },
            { key: "body", header: "文案", render: (row) => <span className="admin-clip">{row.body_template}</span> },
            { key: "status", header: "状态", render: (row) => <Badge tone={statusTone(row.status)}>{row.status}</Badge> },
            {
              key: "actions",
              header: "审核",
              render: (row) =>
                row.status === "draft" ? (
                  <div className="admin-inline">
                    <Button size="sm" variant="primary" onClick={() => void review(row, true)}>通过</Button>
                    <Button size="sm" variant="danger" onClick={() => void review(row, false)}>驳回</Button>
                  </div>
                ) : (
                  <span className="admin-muted">{row.reviewed_by || "—"}</span>
                ),
            },
          ]}
          empty={<EmptyState title="还没有素材" description="先选活动，再提交一条聊天/动态素材。" />}
        />
      </div>
    </div>
  );
}

/* ------------------------------------------------------------- 角色分配 */

function AssignmentsPanel({
  campaignId,
  campaignOptions,
  assignments,
  push,
  onSelect,
  onChanged,
}: {
  campaignId: string;
  campaignOptions: Array<{ value: string; label: string }>;
  assignments: Assignment[];
  push: (message: string, tone?: "info" | "error") => void;
  onSelect: (id: string) => void;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);

  async function suggest() {
    if (!campaignId) {
      push("先选择活动", "error");
      return;
    }
    setBusy(true);
    try {
      await apiPost(
        withQuery(`/api/admin/ads/campaigns/${encodeURIComponent(campaignId)}/assignments/suggest`, { limit: 10 }),
      );
      push("已生成建议（需确认后才可投放）");
      onChanged();
    } catch (err) {
      push(errText(err), "error");
    } finally {
      setBusy(false);
    }
  }

  async function confirm(personaId: string, confirmed: boolean) {
    try {
      await apiPost(
        withQuery(
          `/api/admin/ads/campaigns/${encodeURIComponent(campaignId)}/assignments/${encodeURIComponent(personaId)}/confirm`,
          { confirmed },
        ),
      );
      onChanged();
    } catch (err) {
      push(errText(err), "error");
    }
  }

  return (
    <div className="admin-card">
      <h2 className="admin-card-title">角色分配</h2>
      <p className="admin-muted">
        只有「已确认」的人物才会在聊天里投放该活动的广告；建议分数来自人物兴趣/职业与该活动标签的匹配。
      </p>
      <div className="admin-inline">
        <Select value={campaignId} onChange={onSelect} options={campaignOptions} />
        <Button variant="primary" onClick={() => void suggest()} loading={busy}>
          生成建议
        </Button>
      </div>
      <Table
        rows={assignments}
        rowKey={(row) => row.id}
        columns={[
          { key: "persona", header: "人物", render: (row) => <code className="admin-code">{row.persona_id}</code> },
          { key: "score", header: "匹配分", numeric: true, render: (row) => row.score.toFixed(1) },
          { key: "status", header: "状态", render: (row) => <Badge tone={statusTone(row.status)}>{row.status}</Badge> },
          {
            key: "actions",
            header: "操作",
            render: (row) => (
              <div className="admin-inline">
                {row.status !== "confirmed" ? (
                  <Button size="sm" variant="primary" onClick={() => void confirm(row.persona_id, true)}>确认</Button>
                ) : null}
                {row.status !== "rejected" ? (
                  <Button size="sm" variant="ghost" onClick={() => void confirm(row.persona_id, false)}>拒绝</Button>
                ) : null}
              </div>
            ),
          },
        ]}
        empty={<EmptyState title="还没有分配" description="选活动 → 生成建议 → 确认人物。" />}
      />
    </div>
  );
}

/* ------------------------------------------------------------- 投放记录 */

function DeliveriesPanel({
  campaignId,
  campaignOptions,
  deliveries,
  onSelect,
  onRefresh,
}: {
  campaignId: string;
  campaignOptions: Array<{ value: string; label: string }>;
  deliveries: Delivery[];
  onSelect: (id: string) => void;
  onRefresh: () => void;
}) {
  return (
    <div className="admin-card">
      <h2 className="admin-card-title">投放记录（最近 {deliveries.length} 条）</h2>
      <p className="admin-muted">
        当前免登录版本没有「成年 + 主动开启」的账号，因此这里应为空；等账号恢复后才会有真实投放。
      </p>
      <div className="admin-inline">
        <Select value={campaignId} onChange={onSelect} options={campaignOptions} />
        <Button variant="ghost" onClick={onRefresh}>刷新</Button>
      </div>
      <Table
        rows={deliveries}
        rowKey={(row) => row.id}
        columns={[
          { key: "id", header: "投放 ID", render: (row) => <code className="admin-code">{row.id}</code> },
          { key: "user", header: "用户", render: (row) => row.user_id },
          { key: "channel", header: "渠道", render: (row) => row.channel },
          { key: "status", header: "状态", render: (row) => <Badge tone={statusTone(row.status)}>{row.status}</Badge> },
          { key: "time", header: "预留时间", render: (row) => row.reserved_at },
        ]}
        empty={<EmptyState title="暂无投放记录" description="账号恢复且用户开启合作推荐后才会产生。" />}
      />
    </div>
  );
}
