"use client";

/**
 * 设置 · 合作推荐（免登录版本的广告开关）。
 *
 * 服务端约束：
 * - 默认关闭，只有**成年**用户能主动开启；
 * - 免登录期间按资料里的生日自报成年判定；恢复邮箱登录后要求已核验成年账号；
 * - 未成年人永远不投放（即使打开开关，服务端也不会返回广告）。
 */

import { useCallback, useEffect, useState } from "react";
import { Sparkles } from "lucide-react";

import { adStatus, setAdsOptIn, type AdStatus } from "../v3/accountClient";

const cardStyle: React.CSSProperties = {
  display: "grid",
  gap: 10,
  padding: "14px 16px",
  borderRadius: 14,
  border: "1px solid rgba(120,120,120,.18)",
};

const muted: React.CSSProperties = { fontSize: 12, opacity: 0.72, lineHeight: 1.6 };

function reasonText(status: AdStatus): string {
  const reason = status.can_opt_in ? "" : status.opt_in_reason || status.reason;
  switch (reason) {
    case "no_account":
      return "当前版本需要登录账号后才能开启；免登录模式下只有填了成年生日的用户可以开启。";
    case "not_adult_verified":
      return "只有成年用户可以开启推荐。请先在个人资料里填写真实生日（14–17 岁为青少年模式，不展示广告）。";
    case "ads_not_allowed":
      return "当前账号不可用合作推荐能力。";
    case "ads_disabled":
      return "广告能力尚未启用。";
    case "ads_opt_out":
      return "已开启但还未打开开关。";
    default:
      return "暂时不能开启合作推荐。";
  }
}

export function RecommendAdsSection() {
  const [status, setStatus] = useState<AdStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      setStatus(await adStatus());
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function toggle(next: boolean) {
    setBusy(true);
    setError("");
    try {
      await setAdsOptIn(next);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={cardStyle}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 600 }}>
        <Sparkles size={18} strokeWidth={1.8} />
        合作推荐
      </div>
      <p style={muted}>
        偶尔在聊天或动态里出现标注「广告 · 品牌合作」的内容；默认关闭，随时可以关掉。
        关闭或举报会让该活动在你这里屏蔽 30 天。未成年人不会看到这类内容。
      </p>

      {status?.can_opt_in ? (
        <label style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 13 }}>
          <input
            type="checkbox"
            checked={Boolean(status.ads_opt_in)}
            disabled={busy}
            onChange={(event) => void toggle(event.target.checked)}
          />
          接收合作推荐（{status.age_gate === "adult" ? "成年" : status.age_gate}）
        </label>
      ) : (
        <p style={{ ...muted, margin: 0 }}>{status ? reasonText(status) : "正在读取状态…"}</p>
      )}

      {status?.eligible ? (
        <p style={{ ...muted, margin: 0 }}>已开启：推荐流第 4 条后、或聊天里需求匹配时才可能出现。</p>
      ) : null}
      {error ? <p style={{ ...muted, margin: 0, color: "#d33" }}>{error}</p> : null}
    </div>
  );
}
