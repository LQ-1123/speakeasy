"use client";

/**
 * 管理台外壳：侧栏 + 顶栏 + hash 路由订阅。
 *
 * 侧栏按"四域"分组（用户 / AI 世界 / 捏人 / 系统），总览单独置顶。
 * 未交付的分区标灰而不是隐藏——运营能提前看到全貌，也不会点进空白页。
 */

import { useCallback, useEffect, useState } from "react";

import type { AdminRoute } from "./router";
import {
  ADMIN_NAV,
  DOMAIN_LABEL,
  activeNavItem,
  adminRouteToHash,
  isImplemented,
  normalizeAdminHash,
  parseAdminRoute,
} from "./router";
import { logout } from "./api";

/**
 * 订阅地址栏 hash，返回当前路由与导航函数。
 * URL 是唯一事实来源：组件只表达"我要去哪"，不直接改 history 语义。
 */
export function useAdminRoute(): [AdminRoute, (route: AdminRoute) => void] {
  const [hash, setHash] = useState<string>("");

  useEffect(() => {
    // 首帧统一到规范化 hash（空 hash → #/overview），避免刷新后停在空白。
    const normalized = normalizeAdminHash(window.location.hash);
    if (normalized !== window.location.hash) {
      window.history.replaceState(null, "", normalized);
    }
    setHash(normalized);
    const onHashChange = () => setHash(normalizeAdminHash(window.location.hash));
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  const navigate = useCallback((route: AdminRoute) => {
    const next = adminRouteToHash(route);
    if (next !== window.location.hash) window.location.hash = next;
  }, []);

  return [parseAdminRoute(hash), navigate];
}

export function Sidebar({ current }: { current: AdminRoute }) {
  const active = activeNavItem(current);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  const go = (hash: string) => {
    window.location.hash = hash;
  };

  return (
    <aside className="admin-sidebar">
      <div className="admin-sidebar-brand">
        <div>
          <div className="admin-sidebar-brand-text">Sonder</div>
          <div className="admin-sidebar-brand-sub">运营控制台</div>
        </div>
      </div>
      <nav className="admin-nav" aria-label="管理台导航">
        {ADMIN_NAV.map((group) => {
          const isCollapsed = collapsed[group.domain] === true;
          /** 总览只有一个入口，不做折叠。 */
          const collapsible = group.items.length > 1;
          return (
            <div key={group.domain}>
              <div className="admin-nav-group-label">
                {collapsible ? (
                  <button
                    type="button"
                    className="admin-nav-group-toggle"
                    onClick={() =>
                      setCollapsed((state) => ({ ...state, [group.domain]: !isCollapsed }))
                    }
                    aria-expanded={!isCollapsed}
                  >
                    {isCollapsed ? "▸" : "▾"} {DOMAIN_LABEL[group.domain]}
                  </button>
                ) : (
                  DOMAIN_LABEL[group.domain]
                )}
              </div>
              {isCollapsed
                ? null
                : group.items.map((item) => {
                    const isActive = active === item;
                    // 以"有没有真实页面"为准，而不是计划里的期数：
                    // 期数是排期，页面是否存在才是用户能不能点。
                    const delivered = isImplemented(item.route);
                    return (
                      <button
                        type="button"
                        key={item.key}
                        className="admin-nav-item"
                        aria-current={isActive ? "page" : undefined}
                        data-coming-soon={delivered ? undefined : "true"}
                        title={
                          delivered
                            ? item.hint
                            : `${item.hint || ""}（第 ${item.phase} 期上线）`
                        }
                        disabled={!delivered}
                        onClick={() => {
                          if (delivered) go(adminRouteToHash(item.route));
                        }}
                      >
                        <span>{item.label}</span>
                        {!delivered ? (
                          <span className="admin-nav-item-badge">P{item.phase}</span>
                        ) : null}
                      </button>
                    );
                  })}
            </div>
          );
        })}
      </nav>
      {/* 侧栏底部不再挂「旧版管理台」入口：第 4 期已删除 page.py 与 /admin/legacy，
          留一个死链只会让人以为还有回退通道。 */}
      <div className="admin-sidebar-foot">
        <span className="admin-muted" style={{ fontSize: 12 }}>
          Sonder 运营控制台
        </span>
      </div>
    </aside>
  );
}

export function Topbar({ title }: { title: string }) {
  return (
    <header className="admin-topbar">
      <div className="admin-topbar-title">{title}</div>
      <div className="admin-topbar-spacer" />
      <button type="button" className="admin-btn" data-variant="ghost" onClick={() => void logout()}>
        退出
      </button>
    </header>
  );
}
