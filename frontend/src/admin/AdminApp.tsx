"use client";

/**
 * 管理台入口：外壳 + 分区懒加载。
 *
 * 为什么用 next/dynamic 而不是路由级分割：
 * 管理台是**单页 + hash 路由**（静态导出下没有服务端路由），
 * 所有分区打进同一个 chunk 会让首包随分区数量线性增长。
 * dynamic 让每个分区独立成 chunk，切到哪个才下载哪个。
 */

import dynamic from "next/dynamic";
import { QueryClientProvider } from "@tanstack/react-query";

import { queryClient } from "../w4/queryClient";
import "./admin.css";
import { Sidebar, Topbar, useAdminRoute } from "./shell";
import type { AdminRoute } from "./router";
import { DOMAIN_LABEL, activeNavItem, domainOf } from "./router";
import { LoadingState } from "./ui";

/* 每个分区独立 chunk：切到哪个才下载哪个。 */
const OverviewSection = dynamic(() => import("./sections/OverviewSection"), {
  ssr: false,
  loading: () => <LoadingState />,
});
const JobsSection = dynamic(() => import("./sections/JobsSection"), {
  ssr: false,
  loading: () => <LoadingState />,
});
const ConfigSection = dynamic(() => import("./sections/ConfigSection"), {
  ssr: false,
  loading: () => <LoadingState />,
});
const ReleaseSection = dynamic(() => import("./sections/ReleaseSection"), {
  ssr: false,
  loading: () => <LoadingState />,
});
const MediaSection = dynamic(() => import("./sections/MediaSection"), {
  ssr: false,
  loading: () => <LoadingState />,
});
const DnaSection = dynamic(() => import("./sections/DnaSection"), {
  ssr: false,
  loading: () => <LoadingState />,
});
const ScenesSection = dynamic(() => import("./sections/ScenesSection"), {
  ssr: false,
  loading: () => <LoadingState />,
});
const RhythmSection = dynamic(() => import("./sections/RhythmSection"), {
  ssr: false,
  loading: () => <LoadingState />,
});
const PoolSection = dynamic(() => import("./sections/PoolSection"), {
  ssr: false,
  loading: () => <LoadingState />,
});
const StudioSection = dynamic(() => import("./sections/StudioSection"), {
  ssr: false,
  loading: () => <LoadingState />,
});
const VoicesSection = dynamic(() => import("./sections/VoicesSection"), {
  ssr: false,
  loading: () => <LoadingState />,
});
const ComingSoonSection = dynamic(() => import("./sections/ComingSoonSection"), {
  ssr: false,
});
const UsersSection = dynamic(() => import("./sections/UsersSection"), {
  ssr: false,
  loading: () => <LoadingState />,
});
const UserDetailSection = dynamic(() => import("./sections/UserDetailSection"), {
  ssr: false,
  loading: () => <LoadingState />,
});
const PostsSection = dynamic(() => import("./sections/PostsSection"), {
  ssr: false,
  loading: () => <LoadingState />,
});
const EventsSection = dynamic(() => import("./sections/EventsSection"), {
  ssr: false,
  loading: () => <LoadingState />,
});
const PromptSection = dynamic(() => import("./sections/PromptSection"), {
  ssr: false,
  loading: () => <LoadingState />,
});
const AuditSection = dynamic(() => import("./sections/AuditSection"), {
  ssr: false,
  loading: () => <LoadingState />,
});

function sectionFor(route: AdminRoute) {
  switch (route.name) {
    case "overview":
      return <OverviewSection />;
    case "systemJobs":
      return <JobsSection />;
    case "systemConfig":
      return <ConfigSection />;
    case "systemRelease":
      return <ReleaseSection />;
    case "worldMedia":
      return <MediaSection />;
    case "personaDna":
      return <DnaSection />;
    case "personaScenes":
      return <ScenesSection />;
    case "worldRhythm":
      return <RhythmSection />;
    case "personaPool":
      return <PoolSection />;
    case "personaStudio":
      return <StudioSection />;
    case "personaVoices":
      return <VoicesSection />;
    // P3
    case "users":
      return <UsersSection />;
    case "userDetail":
      return <UserDetailSection userId={route.userId} />;
    case "worldEvents":
      return <EventsSection />;
    case "worldPosts":
      return <PostsSection />;
    case "personaPrompts":
      return <PromptSection />;
    // P4
    case "systemAudit":
      return <AuditSection />;
    default:
      // 所有分区都已交付（P4）；走到这里只能是未知 hash，由兜底组件提示。
      return <ComingSoonSection />;
  }
}

function AdminShell() {
  const [route] = useAdminRoute();
  const item = activeNavItem(route);
  const title = item ? item.label : DOMAIN_LABEL[domainOf(route)];

  return (
    <div className="admin-root">
      <Sidebar current={route} />
      <div className="admin-main">
        <Topbar title={title} />
        <div className="admin-content">{sectionFor(route)}</div>
      </div>
    </div>
  );
}

export default function AdminApp() {
  return (
    <QueryClientProvider client={queryClient}>
      <AdminShell />
    </QueryClientProvider>
  );
}
