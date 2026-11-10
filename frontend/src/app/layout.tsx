import type { Metadata, Viewport } from "next";

import "./globals.css";
import "../theme/tokens.css";
import { THEME_BOOTSTRAP_SCRIPT } from "../theme/bootstrap";

export const metadata: Metadata = {
  title: "Sonder · Every life is a world",
  description: "Sonder：异步语音条的 AI 语伴。捏一个外国朋友，用语音条聊天，顺手把表达变地道。",
  applicationName: "Sonder",
  manifest: "/manifest.webmanifest",
  appleWebApp: {
    capable: true,
    statusBarStyle: "default",
    title: "Sonder",
  },
  icons: {
    icon: [
      { url: "/favicon.ico", sizes: "any" },
      { url: "/icons/brand-32.png", sizes: "32x32", type: "image/png" },
      { url: "/icons/brand-192.png", sizes: "192x192", type: "image/png" },
      { url: "/icons/brand-512.png", sizes: "512x512", type: "image/png" },
    ],
    apple: [{ url: "/icons/brand-180.png", sizes: "180x180", type: "image/png" }],
  },
};

export const viewport: Viewport = {
  themeColor: "#f3f1fa",
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  /* 键盘弹起时让布局视口跟着缩放：配合 platform.ts 的 visualViewport IME 变量，
     Android WebView（adjustNothing）不会再把 composer 挡在键盘下面。 */
  interactiveWidget: "resizes-content",
};

/* 开屏样式直接内联：静态导出时先于任何 CSS 生效，避免白色闪屏。 */
const SPLASH_CSS = `
#sonder-splash{position:fixed;inset:0;z-index:2147483000;display:flex;align-items:center;justify-content:center;overflow:hidden;
  background:linear-gradient(168deg,#e9edfb 0%,#f4f1fb 40%,#fdf3ef 68%,#eef1fb 100%);
  transition:opacity .32s ease,visibility .32s ease}
#sonder-splash.is-done{opacity:0;visibility:hidden;pointer-events:none}
#sonder-splash .son-glow{position:absolute;border-radius:50%;filter:blur(38px);opacity:.55}
#sonder-splash .son-glow.g1{width:46vw;height:46vw;left:-14vw;top:-10vw;background:radial-gradient(circle,#aab9f7 0%,rgba(170,185,247,0) 70%)}
#sonder-splash .son-glow.g2{width:52vw;height:52vw;right:-18vw;top:-6vw;background:radial-gradient(circle,#cfd4fb 0%,rgba(207,212,251,0) 70%)}
#sonder-splash .son-glow.g3{width:60vw;height:60vw;left:-20vw;top:38vh;background:radial-gradient(circle,#f8d7ce 0%,rgba(248,215,206,0) 70%)}
#sonder-splash .son-brand{position:relative;z-index:2;display:flex;flex-direction:column;align-items:center;transform:translateY(-9vh)}
#sonder-splash .son-brand img{width:104px;height:104px;filter:drop-shadow(0 20px 34px rgba(83,88,150,.18))}
#sonder-splash .son-title{margin-top:20px;font:600 26px/1.1 -apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  letter-spacing:.42em;padding-left:.42em;color:#232946}
#sonder-splash .son-tagline{margin-top:10px;font:400 12.5px/1.5 -apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  letter-spacing:.07em;color:#6c7090}
#sonder-splash .son-hills{position:absolute;left:0;right:0;bottom:0;height:32vh;z-index:1;display:block}
@media (prefers-color-scheme:dark){
  #sonder-splash{background:linear-gradient(168deg,#191b28 0%,#15161f 42%,#1e1a24 70%,#141624 100%)}
  #sonder-splash .son-glow{opacity:.28}
  #sonder-splash .son-title{color:#f3f2f9}
  #sonder-splash .son-tagline{color:#9a9db8}
}
`;

const SPLASH_SCRIPT = `
(function(){
  var el=document.getElementById('sonder-splash');
  if(!el) return;
  var started=Date.now(); var done=false;
  function hide(){ if(done) return; done=true;
    var wait=Math.max(0,1150-(Date.now()-started));
    setTimeout(function(){ el.classList.add('is-done');
      setTimeout(function(){ if(el.parentNode) el.parentNode.removeChild(el); },340); }, wait); }
  window.__hideSonderSplash=hide;
  window.addEventListener('load',function(){ hide(); });
  setTimeout(hide,5000);
})();
`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOTSTRAP_SCRIPT }} />
        <style dangerouslySetInnerHTML={{ __html: SPLASH_CSS }} />
      </head>
      <body>
        {/* 原生启动图之后的品牌开屏：渐变夜空 + 山丘 + SONDER，React 挂载即淡出。 */}
        <div id="sonder-splash" aria-hidden="true">
          <span className="son-glow g1" />
          <span className="son-glow g2" />
          <span className="son-glow g3" />
          <div className="son-brand">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src="/icons/sonder-mark.png" alt="" width={104} height={104} />
            <div className="son-title">SONDER</div>
            <div className="son-tagline">Every life is a world.</div>
          </div>
          <svg className="son-hills" viewBox="0 0 390 240" preserveAspectRatio="none" aria-hidden="true">
            <defs>
              <linearGradient id="son-hill-far" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0" stopColor="#8f9bd8" />
                <stop offset="1" stopColor="#6d78bc" />
              </linearGradient>
              <linearGradient id="son-hill-near" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0" stopColor="#59619b" />
                <stop offset="1" stopColor="#3c4377" />
              </linearGradient>
              <linearGradient id="son-hill-teal" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0" stopColor="#93b7c6" />
                <stop offset="1" stopColor="#7f9fb8" />
              </linearGradient>
            </defs>
            <path d="M0 128 C58 96 116 150 196 120 C268 94 322 138 390 108 L390 240 L0 240 Z" fill="url(#son-hill-far)" />
            <path d="M0 168 C70 142 132 186 216 158 C288 134 344 170 390 154 L390 240 L0 240 Z" fill="url(#son-hill-teal)" opacity=".55" />
            <path d="M0 190 C82 158 168 206 262 178 C318 162 360 178 390 172 L390 240 L0 240 Z" fill="url(#son-hill-near)" />
            {/* 站在山脊上的小小身影：Sonder 的「每个人都是一个世界」 */}
            <g fill="#272b49">
              <circle cx="197" cy="146" r="5.2" />
              <path d="M197 152 C191 153 188 159 188 168 L185 186 L190 186 L191 174 L191 190 L195 190 L196 174 L198 174 L199 190 L203 190 L203 174 L204 186 L209 186 L206 168 C206 159 203 153 197 152 Z" />
            </g>
          </svg>
        </div>
        {children}
        <script dangerouslySetInnerHTML={{ __html: SPLASH_SCRIPT }} />
      </body>
    </html>
  );
}
