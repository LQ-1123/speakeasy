"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Check,
  Contrast,
  Image as ImageIcon,
  Monitor,
  Moon,
  RotateCcw,
  Smartphone,
  Sun,
  Upload,
  Wand2,
  X,
} from "lucide-react";

import "./preview.css";
import { ImageThemeService, type ImageThemeResult } from "@/theme/image-theme";
import {
  contrastChecks,
  honorSnapshot,
  makeSnapshot,
  palettePayloadToSnapshot,
  validatePalettePayload,
  validateSnapshot,
} from "@/theme/palette";
import type { ThemePreference, ThemeSnapshot } from "@/theme/types";
import { THEME_ROLES, type ThemeColors } from "@/theme/types";
import type { PreviewCommand, PreviewEvent, PreviewMessage } from "@/w4/previewProtocol";

const CHANNEL = "speakeasy-design-preview";
const IFRAME_ORIGIN = typeof window === "undefined" ? "" : window.location.origin;

type Variant = "normal" | "loading" | "empty" | "error" | "long" | "call";
const VARIANTS: Array<{ value: Variant; label: string }> = [
  { value: "normal", label: "普通" },
  { value: "loading", label: "加载中" },
  { value: "empty", label: "空数据" },
  { value: "error", label: "失败" },
  { value: "long", label: "长列表" },
  { value: "call", label: "通话中" },
];

const PHONE_WIDTHS = [320, 360, 390, 430];
const TABLET_WIDTHS = [768, 1024, 1440];

const FONT_SCALES = ["0.85", "1", "1.3", "1.6", "2"];

const TEST_COLORS = [
  { name: "黑白", color: "#3B3B3B" },
  { name: "低饱和", color: "#8A8178" },
  { name: "红", color: "#B3261E" },
  { name: "橙", color: "#C25E00" },
  { name: "绿", color: "#2E6B3E" },
  { name: "蓝", color: "#345A9E" },
  { name: "紫", color: "#6B4C9A" },
];

const ROUTES = [
  { value: "#/chats", label: "消息列表" },
  { value: "#/chat/fixture-1", label: "聊天" },
  { value: "#/discover", label: "发现" },
  { value: "#/discoverPeople", label: "偶遇" },
  { value: "#/persona/persona-1", label: "人物主页" },
  { value: "#/events", label: "通知中心" },
  { value: "#/me", label: "我" },
  { value: "#/expressions", label: "表达本" },
  { value: "#/settings", label: "设置" },
];

interface FrameMetrics {
  viewport: { width: number; height: number };
  scroll: { width: number; height: number };
  overflow: Array<{ tag: string; className: string; right: number; left: number }>;
  longTasks: number[];
  fps: number;
  nodes: number;
  images: number;
  theme: { label: string; mode: string; fingerprint: string };
}

interface HapticLine {
  kind: string;
  performed: boolean;
  platform: string;
  at: number;
}

interface FrameState {
  ready: boolean;
  metrics: FrameMetrics | null;
  themeLabel: string;
}

export default function DesignPreviewPage() {
  const [variant, setVariant] = useState<Variant>("normal");
  const [route, setRoute] = useState("#/chats");
  const [phoneWidth, setPhoneWidth] = useState(390);
  const [desktopWidth, setDesktopWidth] = useState(1440);
  const [mode, setMode] = useState<ThemePreference>("light");
  const [snapshot, setSnapshot] = useState<ThemeSnapshot>(() => honorSnapshot(1));
  const [transparency, setTransparency] = useState(true);
  const [reducedMotion, setReducedMotion] = useState(false);
  const [fontScale, setFontScale] = useState("1");
  const [safeTop, setSafeTop] = useState(0);
  const [safeBottom, setSafeBottom] = useState(0);
  const [ime, setIme] = useState(0);
  const [reloadKey, setReloadKey] = useState(0);
  const [desktopFrame, setDesktopFrame] = useState<FrameState>({ ready: false, metrics: null, themeLabel: "" });
  const [phoneFrame, setPhoneFrame] = useState<FrameState>({ ready: false, metrics: null, themeLabel: "" });
  const [haptics, setHaptics] = useState<HapticLine[]>([]);
  const [imageResult, setImageResult] = useState<ImageThemeResult | null>(null);
  const [imageBusy, setImageBusy] = useState(false);
  const [imageError, setImageError] = useState("");
  const [candidateColors, setCandidateColors] = useState<string[]>([]);
  const [audit, setAudit] = useState<Array<{ name: string; color: string; pass: boolean; checks: ReturnType<typeof contrastChecks> }>>([]);
  const [auditBusy, setAuditBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const desktopRef = useRef<HTMLIFrameElement>(null);
  const phoneRef = useRef<HTMLIFrameElement>(null);
  const service = useRef<ImageThemeService | null>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const [desktopScale, setDesktopScale] = useState(0.62);
  const [phoneScale, setPhoneScale] = useState(0.72);

  const iframeSrc = `/?preview=1&state=${variant}${route}`;

  const flash = useCallback((text: string) => {
    setNotice(text);
    window.setTimeout(() => setNotice((current) => (current === text ? "" : current)), 2200);
  }, []);

  const post = useCallback((frame: HTMLIFrameElement | null, payload: PreviewCommand) => {
    if (!frame?.contentWindow) return;
    const message: PreviewMessage<PreviewCommand> = { channel: CHANNEL, payload };
    frame.contentWindow.postMessage(message, IFRAME_ORIGIN);
  }, []);

  const broadcast = useCallback(
    (payload: PreviewCommand) => {
      post(desktopRef.current, payload);
      post(phoneRef.current, payload);
    },
    [post],
  );

  const sendSnapshot = useCallback(() => {
    broadcast({ type: "theme-snapshot", snapshot });
  }, [broadcast, snapshot]);

  const sendSettings = useCallback(() => {
    broadcast({ type: "settings", transparency, reducedMotion, fontScale: Number(fontScale) });
    broadcast({ type: "environment", safeTop, safeBottom, ime });
  }, [broadcast, fontScale, ime, reducedMotion, safeBottom, safeTop, transparency]);

  const startCall = useCallback(
    (action: "start" | "minimize" | "expand" | "end") => {
      broadcast({ type: "call", action, friendId: "fixture-1" });
    },
    [broadcast],
  );

  useEffect(() => {
    sendSnapshot();
    broadcast({ type: "theme-mode", mode });
  }, [broadcast, mode, sendSnapshot]);

  useEffect(() => {
    sendSettings();
  }, [fontScale, ime, reducedMotion, safeBottom, safeTop, sendSettings, transparency]);

  useEffect(() => {
    if (variant === "call") {
      const timer = window.setTimeout(() => startCall("start"), 520);
      return () => window.clearTimeout(timer);
    }
  }, [startCall, variant]);

  useEffect(() => {
    service.current = new ImageThemeService();
    return () => {
      service.current?.dispose();
      service.current = null;
    };
  }, []);

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return;
      const data = event.data as PreviewMessage<PreviewEvent> | undefined;
      if (!data || data.channel !== CHANNEL) return;
      const source = event.source;
      const target = source === desktopRef.current?.contentWindow ? "desktop" : source === phoneRef.current?.contentWindow ? "phone" : null;
      if (!target) return;
      const setter = target === "desktop" ? setDesktopFrame : setPhoneFrame;
      const payload = data.payload;
      if (payload.type === "ready") {
        setter({ ready: true, metrics: null, themeLabel: payload.theme.label });
        post(target === "desktop" ? desktopRef.current : phoneRef.current, {
          type: "theme-snapshot",
          snapshot,
        });
        post(target === "desktop" ? desktopRef.current : phoneRef.current, {
          type: "theme-mode",
          mode,
        });
        post(target === "desktop" ? desktopRef.current : phoneRef.current, {
          type: "settings",
          transparency,
          reducedMotion,
          fontScale: Number(fontScale),
        });
        post(target === "desktop" ? desktopRef.current : phoneRef.current, {
          type: "environment",
          safeTop,
          safeBottom,
          ime,
        });
        if (variant === "call") {
          window.setTimeout(() => startCall("start"), 240);
        }
      }
      if (payload.type === "theme") {
        setter((current) => ({ ...current, themeLabel: payload.theme.label }));
      }
      if (payload.type === "metrics") {
        setter((current) => ({ ...current, metrics: payload }));
      }
      if (payload.type === "haptic") {
        setHaptics((current) => [
          { kind: payload.kind, performed: payload.performed, platform: payload.platform, at: payload.at },
          ...current,
        ].slice(0, 14));
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [fontScale, ime, mode, post, reducedMotion, safeBottom, safeTop, snapshot, startCall, transparency, variant]);

  useEffect(() => {
    const onResize = () => {
      const width = stageRef.current?.clientWidth ?? 1200;
      const availableForDesktop = Math.max(420, width - Math.round(phoneWidth * 0.82) - 52);
      setDesktopScale(Math.min(0.9, availableForDesktop / desktopWidth));
      setPhoneScale(Math.min(0.9, Math.max(0.55, (width - 28) / 1200)));
    };
    onResize();
    const observer = new ResizeObserver(onResize);
    if (stageRef.current) observer.observe(stageRef.current);
    window.addEventListener("resize", onResize);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", onResize);
    };
  }, [desktopWidth, phoneWidth]);

  const applyPaletteSnapshot = useCallback(
    (next: ThemeSnapshot, message: string) => {
      setSnapshot(next);
      flash(message);
    },
    [flash],
  );

  const onHonorImport = async (file: File) => {
    try {
      const parsed = JSON.parse(await file.text()) as unknown;
      const full = validateSnapshot(parsed);
      if (full.ok && full.snapshot) {
        applyPaletteSnapshot({ ...full.snapshot, revision: Date.now() }, "已导入完整 ThemeSnapshot");
        return;
      }
      const payload = validatePalettePayload(parsed);
      if (!payload.ok || !payload.payload) {
        throw new Error(payload.errors.join("；") || "不是受支持的色板 JSON");
      }
      const next = palettePayloadToSnapshot(payload.payload, Date.now());
      applyPaletteSnapshot(next, "已导入手机色板 JSON");
    } catch (error) {
      flash(error instanceof Error ? error.message : "色板导入失败");
    }
  };

  const onImage = async (file: File) => {
    if (!service.current) return;
    setImageBusy(true);
    setImageError("");
    try {
      const result = await service.current.fromFile(file);
      setImageResult(result);
      setCandidateColors(result.candidates);
      const next = makeSnapshot({
        revision: Date.now(),
        source: "image",
        label: `图片主题 · ${file.name}`,
        palettes: result.palettes,
        capabilities: {
          systemSemanticRoles: true,
          dynamicColor: true,
          transparency: true,
          haptics: false,
          reducedMotion: false,
          contrast: "standard",
        },
        meta: {
          sourceColor: result.sourceColor,
          note: `图片仅在浏览器本地量化；摘要 ${result.digest.slice(0, 12)}…，${result.quantizedSize} 个量化簇。`,
          completeness: "partial",
        },
      });
      applyPaletteSnapshot(next, `已从图片生成主题 · ${result.sourceColor}`);
    } catch (error) {
      setImageError(error instanceof Error ? error.message : "图片主题生成失败");
    } finally {
      setImageBusy(false);
    }
  };

  const pickCandidate = async (color: string) => {
    if (!service.current) return;
    setImageBusy(true);
    try {
      const palettes = await service.current.fromColor(color);
      const next = makeSnapshot({
        revision: Date.now(),
        source: "image",
        label: `图片主题 · ${color}`,
        palettes,
        capabilities: {
          systemSemanticRoles: true,
          dynamicColor: true,
          transparency: true,
          haptics: false,
          reducedMotion: false,
          contrast: "standard",
        },
        meta: { sourceColor: color, completeness: "partial", note: "候选主色 · 浏览器本地 HCT 生成" },
      });
      applyPaletteSnapshot(next, `已应用候选主色 ${color}`);
    } finally {
      setImageBusy(false);
    }
  };

  const runAudit = async () => {
    if (!service.current || auditBusy) return;
    setAuditBusy(true);
    const results: typeof audit = [];
    try {
      for (const item of TEST_COLORS) {
        const palettes = await service.current.fromColor(item.color);
        const lightChecks = contrastChecks(palettes.light);
        const darkChecks = contrastChecks(palettes.dark);
        const checks = [...lightChecks, ...darkChecks];
        results.push({
          name: item.name,
          color: item.color,
          pass: checks.every((check) => check.pass),
          checks,
        });
      }
      setAudit(results);
    } catch (error) {
      flash(error instanceof Error ? error.message : "对比度审计失败");
    } finally {
      setAuditBusy(false);
    }
  };

  const activeColors: ThemeColors = snapshot.palettes[mode === "dark" ? "dark" : "light"];
  const allTestsPass = audit.length > 0 && audit.every((item) => item.pass);
  const frameScaleStyle = useMemo(
    () => ({ width: desktopWidth * desktopScale, height: 900 * desktopScale }),
    [desktopScale, desktopWidth],
  );

  return (
    <main className="preview-root">
      <header className="preview-header">
        <div className="preview-brand">
          <span className="preview-logo">S</span>
          <div>
            <h1>SpeakEasy Design Preview</h1>
            <p>Material 3 Expressive · 系统动态配色 · Web 验收面板</p>
          </div>
        </div>
        <div className="preview-header-actions">
          <span className={`preview-live ${desktopFrame.ready && phoneFrame.ready ? "on" : ""}`}>
            <i /> {desktopFrame.ready && phoneFrame.ready ? "预览已连接" : "正在连接预览"}
          </span>
          <a className="preview-link" href="/#/chats" target="_blank" rel="noreferrer">
            打开正式 Web
          </a>
          <button className="preview-icon" onClick={() => setReloadKey((key) => key + 1)} title="重新加载预览">
            <RotateCcw size={17} />
          </button>
        </div>
      </header>

      <section className="preview-controls" aria-label="预览控制">
        <div className="preview-control-group">
          <label>状态</label>
          <div className="preview-chips">
            {VARIANTS.map((item) => (
              <button key={item.value} className={variant === item.value ? "on" : ""} onClick={() => setVariant(item.value)}>
                {item.label}
              </button>
            ))}
          </div>
        </div>
        <div className="preview-control-group">
          <label>路由</label>
          <select value={route} onChange={(event) => setRoute(event.target.value)}>
            {ROUTES.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </div>
        <div className="preview-control-group">
          <label>主题</label>
          <div className="preview-seg">
            <button className={mode === "light" ? "on" : ""} onClick={() => setMode("light")}>
              <Sun size={14} /> 浅色
            </button>
            <button className={mode === "dark" ? "on" : ""} onClick={() => setMode("dark")}>
              <Moon size={14} /> 深色
            </button>
            <button className={mode === "system" ? "on" : ""} onClick={() => setMode("system")}>
              系统
            </button>
          </div>
        </div>
        <div className="preview-control-group grow">
          <label>当前色板</label>
          <div className="preview-current-palette">
            {[activeColors.primary, activeColors.primaryContainer, activeColors.secondaryContainer, activeColors.tertiaryContainer, activeColors.surfaceContainerLow, activeColors.surface].map((color) => (
              <i key={color} style={{ background: color }} title={color} />
            ))}
            <span>{snapshot.label}</span>
          </div>
        </div>
      </section>

      <section className="preview-stage" ref={stageRef}>
        <div className="preview-frame-card desktop-card">
          <div className="preview-frame-head">
            <span><Monitor size={15} /> 桌面 / 平板 · {desktopWidth}px</span>
            <span className={desktopFrame.metrics?.overflow.length ? "warn" : "ok"}>
              {desktopFrame.metrics ? `${desktopFrame.metrics.overflow.length} 处溢出` : "等待测量"}
            </span>
          </div>
          <div className="preview-frame-clip" style={frameScaleStyle}>
            <iframe
              key={`desktop-${variant}-${route}-${reloadKey}`}
              ref={desktopRef}
              title="桌面预览"
              src={iframeSrc}
              style={{ width: desktopWidth, height: 900, transform: `scale(${desktopScale})` }}
            />
          </div>
          <div className="preview-width-row">
            {TABLET_WIDTHS.map((width) => (
              <button key={width} className={desktopWidth === width ? "on" : ""} onClick={() => setDesktopWidth(width)}>
                {width}px
              </button>
            ))}
            <span>{desktopFrame.themeLabel || "—"}</span>
          </div>
        </div>

        <div className="preview-frame-card phone-card">
          <div className="preview-frame-head">
            <span><Smartphone size={15} /> 手机 · {phoneWidth}px</span>
            <span className={phoneFrame.metrics?.overflow.length ? "warn" : "ok"}>
              {phoneFrame.metrics ? `${phoneFrame.metrics.overflow.length} 处溢出` : "等待测量"}
            </span>
          </div>
          <div
            className="preview-phone-shell"
            style={{
              width: phoneWidth * phoneScale + 16,
              height: 844 * phoneScale + 16,
            }}
          >
            <div
              className="preview-phone-clip"
              style={{ width: phoneWidth * phoneScale, height: 844 * phoneScale }}
            >
              <iframe
                key={`phone-${variant}-${route}-${reloadKey}`}
                ref={phoneRef}
                title="手机预览"
                src={iframeSrc}
                style={{ width: phoneWidth, height: 844, transform: `scale(${phoneScale})` }}
              />
            </div>
          </div>
          <div className="preview-width-row">
            {PHONE_WIDTHS.map((width) => (
              <button key={width} className={phoneWidth === width ? "on" : ""} onClick={() => setPhoneWidth(width)}>
                {width}
              </button>
            ))}
          </div>
        </div>
      </section>

      <section className="preview-lower">
        <div className="preview-panel">
          <div className="preview-panel-head">
            <h2>手机色板</h2>
            <span className="preview-badge">ADB 真机</span>
          </div>
          <p className="preview-note">
            荣耀 500 Pro · Android 17 / MagicOS 11 · 来源 home_wallpaper。主色与容器角色为真机读取值；其余角色按同源补全。
          </p>
          <div className="preview-palette-grid">
            {THEME_ROLES.map((role) => (
              <div key={role} className="preview-swatch">
                <i style={{ background: activeColors[role] }} />
                <span>{role}</span>
                <code>{activeColors[role]}</code>
              </div>
            ))}
          </div>
          <div className="preview-row">
            <label className="preview-file">
              <Upload size={15} /> 导入手机色板 JSON
              <input
                type="file"
                accept="application/json,.json"
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  event.target.value = "";
                  if (file) void onHonorImport(file);
                }}
              />
            </label>
            <button className="preview-small-btn" onClick={() => setSnapshot(honorSnapshot(Date.now()))}>
              <RotateCcw size={14} /> 恢复荣耀色板
            </button>
          </div>
        </div>

        <div className="preview-panel">
          <div className="preview-panel-head">
            <h2>图片主题探索</h2>
            <span className="preview-badge soft">本地 Worker</span>
          </div>
          <p className="preview-note">
            128px 解码 → QuantizerCelebi → Score → HCT / SchemeTonalSpot。仅用于设计测试，不代表手机 Monet 的最终输出。
          </p>
          <label className="preview-drop">
            <ImageIcon size={20} />
            <span>{imageBusy ? "正在量化…" : imageResult ? "再选一张图片" : "选择本地图片（不上传）"}</span>
            <input
              type="file"
              accept="image/jpeg,image/png,image/webp,image/gif"
              onChange={(event) => {
                const file = event.target.files?.[0];
                event.target.value = "";
                if (file) void onImage(file);
              }}
            />
          </label>
          {imageError ? <p className="preview-error">{imageError}</p> : null}
          {candidateColors.length ? (
            <div className="preview-candidates">
              {candidateColors.map((color) => (
                <button key={color} onClick={() => void pickCandidate(color)} title={color}>
                  <i style={{ background: color }} />
                  <code>{color}</code>
                </button>
              ))}
            </div>
          ) : null}
          {imageResult ? (
            <p className="preview-digest">
              摘要 {imageResult.digest.slice(0, 16)}… · {imageResult.quantizedSize} 个量化簇
            </p>
          ) : null}
          <div className="preview-row">
            <button className="preview-small-btn" onClick={() => void runAudit()} disabled={auditBusy}>
              <Contrast size={14} /> {auditBusy ? "检查中…" : "运行 7 组主题对比度审计"}
            </button>
            {audit.length ? <span className={allTestsPass ? "preview-pass" : "preview-warn"}>{allTestsPass ? "全部通过" : "存在未通过主题"}</span> : null}
          </div>
          {audit.length ? (
            <div className="preview-audit">
              {audit.map((item) => (
                <button key={item.name} className={item.pass ? "pass" : "fail"} onClick={() => void pickCandidate(item.color)} title={item.checks.filter((check) => !check.pass).map((check) => `${check.role} ${check.ratio.toFixed(2)}:1`).join("\n")}>
                  <i style={{ background: item.color }} />
                  <span>{item.name}</span>
                  {item.pass ? <Check size={13} /> : <X size={13} />}
                </button>
              ))}
            </div>
          ) : null}
        </div>

        <div className="preview-panel">
          <div className="preview-panel-head">
            <h2>布局与动效</h2>
            <span className="preview-badge soft">实时</span>
          </div>
          <div className="preview-form">
            <label>
              <span>安全区顶部 <b>{safeTop}px</b></span>
              <input type="range" min={0} max={60} value={safeTop} onChange={(e) => setSafeTop(Number(e.target.value))} />
            </label>
            <label>
              <span>安全区底部 <b>{safeBottom}px</b></span>
              <input type="range" min={0} max={48} value={safeBottom} onChange={(e) => setSafeBottom(Number(e.target.value))} />
            </label>
            <label>
              <span>模拟键盘 <b>{ime}px</b></span>
              <input type="range" min={0} max={400} step={10} value={ime} onChange={(e) => setIme(Number(e.target.value))} />
            </label>
            <label>
              <span>字体比例</span>
              <select value={fontScale} onChange={(e) => setFontScale(e.target.value)}>
                {FONT_SCALES.map((value) => (
                  <option key={value} value={value}>
                    {value}×
                  </option>
                ))}
              </select>
            </label>
            <label className="preview-toggle">
              <span>透明 / 毛玻璃</span>
              <input type="checkbox" checked={transparency} onChange={(e) => setTransparency(e.target.checked)} />
            </label>
            <label className="preview-toggle">
              <span>减少动态效果</span>
              <input type="checkbox" checked={reducedMotion} onChange={(e) => setReducedMotion(e.target.checked)} />
            </label>
          </div>
          {variant === "call" ? (
            <div className="preview-row">
              <button className="preview-small-btn" onClick={() => startCall("start")}>展开通话</button>
              <button className="preview-small-btn" onClick={() => startCall("minimize")}>缩小通话条</button>
              <button className="preview-small-btn danger" onClick={() => startCall("end")}>结束通话</button>
            </div>
          ) : null}
        </div>

        <div className="preview-panel">
          <div className="preview-panel-head">
            <h2>验收数据</h2>
            <span className="preview-badge soft">自动</span>
          </div>
          <FrameReport title="桌面" frame={desktopFrame} />
          <FrameReport title="手机" frame={phoneFrame} />
          <div className="preview-haptics">
            <h3>触感事件（浏览器不振动）</h3>
            {haptics.length === 0 ? <p className="preview-muted">点击按钮、导航或录音操作后会出现在这里。</p> : null}
            {haptics.map((line, index) => (
              <div key={`${line.at}-${index}`} className="preview-haptic-line">
                <span>{line.kind}</span>
                <small>
                  {line.platform === "android" ? "原生已执行" : "Web 记录，无振动"} · {new Date(line.at).toLocaleTimeString("zh-CN")}
                </small>
              </div>
            ))}
          </div>
        </div>
      </section>

      <footer className="preview-footer">
        <span>同一套组件 / 路由：iframe 内是正式 App，fixture 仅通过预览适配器注入。</span>
        <span>{notice || (audit.length ? (allTestsPass ? "对比度审计通过" : "请修复未通过主题") : "提示：先运行 7 组主题审计")}</span>
      </footer>
    </main>
  );
}

function FrameReport({ title, frame }: { title: string; frame: FrameState }) {
  const metrics = frame.metrics;
  return (
    <div className="preview-report">
      <h3>{title}</h3>
      {metrics ? (
        <>
          <div className="preview-report-grid">
            <span>视口</span><b>{metrics.viewport.width} × {metrics.viewport.height}</b>
            <span>文档宽</span><b className={metrics.scroll.width > metrics.viewport.width ? "warn" : "ok"}>{metrics.scroll.width}px</b>
            <span>渲染节点</span><b>{metrics.nodes}</b>
            <span>图片</span><b>{metrics.images}</b>
            <span>FPS</span><b className={metrics.fps < 45 ? "warn" : "ok"}>{metrics.fps || "—"}</b>
            <span>长任务</span><b className={metrics.longTasks.length ? "warn" : "ok"}>{metrics.longTasks.length === 0 ? "0" : `${metrics.longTasks.length} · 最大 ${Math.round(Math.max(...metrics.longTasks))}ms`}</b>
          </div>
          {metrics.overflow.length ? (
            <details className="preview-overflow">
              <summary>{metrics.overflow.length} 个元素越界</summary>
              {metrics.overflow.slice(0, 8).map((item, index) => (
                <code key={`${item.className}-${index}`}>{item.tag}.{item.className || "(no class)"} · {item.left}…{item.right}</code>
              ))}
            </details>
          ) : (
            <p className="preview-ok-line"><Check size={13} /> 无横向越界</p>
          )}
        </>
      ) : (
        <p className="preview-muted">等待 iframe 上报指标…</p>
      )}
    </div>
  );
}
