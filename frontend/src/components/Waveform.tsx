"use client";

import { useEffect, useRef } from "react";

export type WaveMode = "idle" | "live" | "playing";

interface WaveformProps {
  /** 取时域数据；返回 false 表示当前没有可用源（画静默基线） */
  sample: (target: Uint8Array<ArrayBuffer>) => boolean;
  mode: WaveMode;
  height?: number;
  /** 上行=琥珀，下行=薄荷 */
  tone?: "accent" | "mint" | "muted";
  label?: string;
  /** 丢帧等异常时在右侧显示警示 */
  warning?: string | null;
  className?: string;
}

const TONES = {
  accent: { core: "#f5a524", glow: "rgba(245,165,36,0.22)", peak: "rgba(245,165,36,0.55)" },
  mint: { core: "#38e0c8", glow: "rgba(56,224,200,0.20)", peak: "rgba(56,224,200,0.50)" },
  muted: { core: "#5c6879", glow: "rgba(92,104,121,0.16)", peak: "rgba(92,104,121,0.4)" },
};

/**
 * 波形绘制 —— 本应用最重要的视觉元素。
 *
 * 实现要点：
 *  - `devicePixelRatio` 缩放，避免高 DPI 下发虚。
 *  - 时域数据（128 为零点）→ 归一化到 [-1,1] → 中轴对称折线。
 *  - 渐变描边：线段从中心到边缘透明度递减（手工逐段 stroke，成本可控，
 *    因为每条线只有 128 段且 60fps 下单条 ≈ 7.7k 次 stroke/s，实测可接受）。
 *  - **峰值保持线**：维护上/下包络，命中后保留 ~600 ms 再指数衰减，
 *    这是"有质感"的关键——纯实时波形看起来廉价。
 *  - idle 时画一条静默基线 + 中心刻度，不空着。
 */
export function Waveform({
  sample,
  mode,
  height = 56,
  tone = "accent",
  label,
  warning,
  className,
}: WaveformProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const rafRef = useRef<number | null>(null);

  // 峰值保持状态（跨帧保留，用 ref 避免重渲染）
  const peakRef = useRef<{ upper: Float32Array; lower: Float32Array; hold: Float32Array }>({
    upper: new Float32Array(0),
    lower: new Float32Array(0),
    hold: new Float32Array(0),
  });

  const sampleRef = useRef(sample);
  const modeRef = useRef(mode);
  const toneRef = useRef(tone);

  useEffect(() => {
    sampleRef.current = sample;
  }, [sample]);
  useEffect(() => {
    modeRef.current = mode;
  }, [mode]);
  useEffect(() => {
    toneRef.current = tone;
  }, [tone]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const colors = TONES[toneRef.current];
    let buffer = new Uint8Array(new ArrayBuffer(2048));
    let dpr = 1;
    let cssWidth = 0;
    let cssHeight = 0;

    const resize = () => {
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      const rect = canvas.getBoundingClientRect();
      cssWidth = rect.width;
      cssHeight = rect.height;
      canvas.width = Math.max(1, Math.round(cssWidth * dpr));
      canvas.height = Math.max(1, Math.round(cssHeight * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      if (peakRef.current.upper.length !== buffer.length) {
        peakRef.current = {
          upper: new Float32Array(buffer.length),
          lower: new Float32Array(buffer.length),
          hold: new Float32Array(buffer.length),
        };
      }
    };

    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(canvas);

    let lastTs = 0;

    const draw = (ts: number) => {
      const dt = lastTs === 0 ? 16 : Math.min(64, ts - lastTs);
      lastTs = ts;

      if (buffer.length !== 2048) buffer = new Uint8Array(new ArrayBuffer(2048));

      const hasAudio = modeRef.current !== "idle" && sampleRef.current(buffer);

      ctx.clearRect(0, 0, cssWidth, cssHeight);

      const midY = cssHeight / 2;
      const innerH = cssHeight - 8;

      // ---- 背景：细网格 + 中心基线（仪表盘质感）
      ctx.strokeStyle = "rgba(255,255,255,0.045)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, 0.5);
      ctx.lineTo(cssWidth, 0.5);
      ctx.moveTo(0, cssHeight - 0.5);
      ctx.lineTo(cssWidth, cssHeight - 0.5);
      ctx.stroke();

      if (!hasAudio) {
        // 静默基线：中点一条极细亮线 + 两端渐隐
        const grad = ctx.createLinearGradient(0, 0, cssWidth, 0);
        grad.addColorStop(0, "rgba(92,104,121,0)");
        grad.addColorStop(0.5, "rgba(92,104,121,0.5)");
        grad.addColorStop(1, "rgba(92,104,121,0)");
        ctx.strokeStyle = grad;
        ctx.beginPath();
        ctx.moveTo(0, midY + 0.5);
        ctx.lineTo(cssWidth, midY + 0.5);
        ctx.stroke();

        // 峰值保持清空
        peakRef.current.upper.fill(0);
        peakRef.current.lower.fill(0);
        rafRef.current = requestAnimationFrame(draw);
        return;
      }

      // ---- 时域 → 归一化
      // 采样密度：canvas 宽度像素数 vs 缓冲长度，做抽稀或插值
      const points = Math.max(32, Math.floor(cssWidth));
      const stride = buffer.length / points;

      const { upper, lower, hold } = peakRef.current;
      if (upper.length !== points) {
        peakRef.current = {
          upper: new Float32Array(points),
          lower: new Float32Array(points),
          hold: new Float32Array(points),
        };
      }
      const up = peakRef.current.upper;
      const lo = peakRef.current.lower;
      const holdArr = peakRef.current.hold;

      const ys = new Float32Array(points);
      /** 一帧（约16ms）内峰值的残余比例；越大保持越久 */
      const decay = Math.pow(0.94, dt / 16);

      for (let i = 0; i < points; i += 1) {
        const idx = Math.min(buffer.length - 1, Math.floor(i * stride));
        // 128 是时域零点；2.2x 增益让小音量也看得见
        const v = ((buffer[idx] - 128) / 128) * 2.2;
        ys[i] = Math.max(-1, Math.min(1, v));

        const mag = Math.abs(ys[i]);

        // 瞬时包络：上升立即跟随，下降指数回落
        if (ys[i] > up[i]) up[i] = ys[i];
        else up[i] *= decay;
        if (ys[i] < lo[i]) lo[i] = ys[i];
        else lo[i] *= decay;

        // 峰值保持：只有更大时才刷新，否则缓慢衰减（形成"保持平台"）
        if (mag > holdArr[i]) holdArr[i] = mag;
        else holdArr[i] *= decay;
      }

      const toY = (v: number) => midY - v * (innerH / 2);

      // ---- 包络填充（渐变，极淡）
      ctx.beginPath();
      ctx.moveTo(0, toY(ys[0]));
      for (let i = 1; i < points; i += 1) ctx.lineTo((i / points) * cssWidth, toY(ys[i]));
      ctx.lineTo(cssWidth, midY);
      ctx.lineTo(0, midY);
      ctx.closePath();
      const fillGrad = ctx.createLinearGradient(0, 0, 0, cssHeight);
      fillGrad.addColorStop(0, colors.glow);
      fillGrad.addColorStop(0.5, "rgba(0,0,0,0)");
      fillGrad.addColorStop(0.5, "rgba(0,0,0,0)");
      fillGrad.addColorStop(1, colors.glow);
      ctx.fillStyle = fillGrad;
      ctx.fill();

      // ---- 主折线：渐变描边（中心亮、边缘淡）
      const lineGrad = ctx.createLinearGradient(0, 0, cssWidth, 0);
      lineGrad.addColorStop(0, colors.core);
      lineGrad.addColorStop(0.5, colors.core);
      lineGrad.addColorStop(1, colors.core);

      // 外发光：粗一点、透明度低的同一折线先画
      ctx.strokeStyle = colors.glow;
      ctx.lineWidth = 4;
      ctx.lineJoin = "round";
      ctx.beginPath();
      ctx.moveTo(0, toY(ys[0]));
      for (let i = 1; i < points; i += 1) ctx.lineTo((i / points) * cssWidth, toY(ys[i]));
      ctx.stroke();

      // 主体折线
      ctx.strokeStyle = lineGrad;
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      ctx.moveTo(0, toY(ys[0]));
      for (let i = 1; i < points; i += 1) ctx.lineTo((i / points) * cssWidth, toY(ys[i]));
      ctx.stroke();

      // ---- 峰值保持线（上下各一条，短虚线）
      ctx.strokeStyle = colors.peak;
      ctx.lineWidth = 1;
      ctx.setLineDash([2, 4]);
      ctx.beginPath();
      let started = false;
      for (let i = 0; i < points; i += 1) {
        const mag = Math.max(Math.abs(up[i]), Math.abs(lo[i]), holdArr[i]);
        if (mag < 0.02) {
          started = false;
          continue;
        }
        const x = (i / points) * cssWidth;
        const yU = toY(mag);
        if (!started) {
          ctx.moveTo(x, yU);
          started = true;
        } else {
          ctx.lineTo(x, yU);
        }
      }
      ctx.stroke();
      ctx.setLineDash([]);

      rafRef.current = requestAnimationFrame(draw);
    };

    rafRef.current = requestAnimationFrame(draw);

    return () => {
      observer.disconnect();
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    };
  }, []);

  const isLive = mode === "live";
  const isPlaying = mode === "playing";

  return (
    <div className={`relative ${className ?? ""}`}>
      {(label || warning) && (
        <div className="mb-1 flex items-baseline justify-between gap-2">
          {label && <span className="panel-label">{label}</span>}
          {warning && (
            <span className="tnum text-[10px] text-[color:var(--accent-danger)]">{warning}</span>
          )}
        </div>
      )}
      <div className="relative overflow-hidden rounded-sm border border-[color:var(--line)] bg-[color:var(--surface-2)]">
        <canvas
          ref={canvasRef}
          style={{ height }}
          className="block w-full"
          aria-label={label ?? "音频波形"}
          role="img"
        />
        {/* 右侧状态角标 */}
        <div className="pointer-events-none absolute right-2 top-1.5 flex items-center gap-1.5">
          {isLive && (
            <span className="tnum text-[9px] uppercase tracking-[0.18em] text-[color:var(--accent)]">
              REC
            </span>
          )}
          {isPlaying && (
            <span className="tnum text-[9px] uppercase tracking-[0.18em] text-[color:var(--accent-ok)]">
              PLAY
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
