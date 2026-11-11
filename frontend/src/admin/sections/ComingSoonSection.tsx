"use client";

/**
 * 未知路由兜底。
 *
 * 历史：第 1~3 期时这个组件叫「即将上线」，用来占位还没交付的分区。
 * 第 4 期所有分区都交付了，它只剩一个用途——**用户输错 / 收藏了失效的 hash**。
 *
 * 所以文案从「安排在第 N 期交付」改成「这里没有页面」：
 * 继续显示"即将上线"会让人以为功能还没做完，然后去等一个永远不会来的东西。
 */

import { adminRouteToHash } from "../router";
import { useAdminRoute } from "../shell";
import { Button, EmptyState } from "../ui";

export default function ComingSoonSection() {
  const [route, navigate] = useAdminRoute();
  return (
    <section className="admin-section">
      <EmptyState
        title="这个地址没有对应页面"
        description={`当前地址指向的分区不存在（${route.name}）。可能是收藏了旧链接，或地址拼错了。`}
        action={
          <Button
            onClick={() => {
              // 与侧栏跳转保持同一套 hash 写法（见 shell.tsx 的 useAdminRoute）
              window.location.hash = adminRouteToHash({ name: "overview" });
              navigate({ name: "overview" });
            }}
          >
            回到总览
          </Button>
        }
      />
    </section>
  );
}
