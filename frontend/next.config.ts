import type { NextConfig } from "next";

/**
 * 静态导出（`output: "export"`）构建，产物 `out/` 由 `scripts/copy-out.mjs`
 * 拷到 `backend/app/static/app/`，由 FastAPI 在**根路径**挂载 StaticFiles。
 *
 * 因此：
 *  - 不加 `basePath` / `assetPrefix`：`/_next/*` 由后端根挂载直接服务。
 *  - 所有 API/WS 调用走**运行时**相对路径，构建期绝不读 env
 *    （否则产物会绑死某个 host，换端口就废）。
 *  - `trailingSlash: true` 让静态目录布局稳定（`/foo/index.html`）。
 */

const isDev = process.env.NODE_ENV === "development";

const nextConfig: NextConfig = {
  output: "export",
  images: { unoptimized: true },
  trailingSlash: true,
  reactStrictMode: true,
  // 关闭左下角 dev 指示器：移动端界面截图/演示时避免遮挡
  devIndicators: false,

  // 开发期专用：由 Next 把 /api、/assets 反代到本地 FastAPI，从而免 CORS。
  // 注意 Next 的 rewrite 不支持 WebSocket，所以 /ws 在 dev 下由
  // lib/useConversation.ts 直连 127.0.0.1:8000（运行时按端口判定）。
  //
  // 关键：静态导出不允许自定义路由，因此非 dev 时**完全不声明** rewrites 键，
  // 否则 `next build` 会报 "rewrites will not automatically work with output: export"。
  ...(isDev
    ? {
        async rewrites() {
          const backend = process.env.BACKEND_ORIGIN ?? "http://127.0.0.1:8000";
          return [
            { source: "/api/:path*", destination: `${backend}/api/:path*` },
            { source: "/assets/:path*", destination: `${backend}/assets/:path*` },
          ];
        },
      }
    : {}),
};

export default nextConfig;
