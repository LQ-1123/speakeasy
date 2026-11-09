import type { ThemeColors } from "./types";

export interface ImageThemeResult {
  digest: string;
  sourceColor: string;
  candidates: string[];
  palettes: { light: ThemeColors; dark: ThemeColors };
  quantizedSize: number;
}

interface WorkerResult {
  kind: "result";
  requestId: number;
  sourceColor: string;
  candidates: string[];
  palettes: { light: ThemeColors; dark: ThemeColors };
  quantizedSize: number;
}

interface WorkerError {
  kind: "error";
  requestId: number;
  message: string;
}

type WorkerReply = WorkerResult | WorkerError;

/**
 * Browser-local image theme exploration.
 * - decodes to at most 128x128
 * - quantizes in a Worker with QuantizerCelebi + Score
 * - caches by SHA-256 digest and by candidate colour
 * - drops stale replies by monotonically increasing request id
 */
export class ImageThemeService {
  private worker: Worker | null = null;
  private pending = new Map<
    number,
    { resolve: (value: ImageThemeResult) => void; reject: (error: Error) => void }
  >();
  private nextId = 1;
  private digestCache = new Map<string, ImageThemeResult>();
  private colorCache = new Map<string, { light: ThemeColors; dark: ThemeColors }>();
  private lastDigest = "";

  private ensureWorker(): Worker {
    if (this.worker) return this.worker;
    this.worker = new Worker(new URL("./image-theme.worker.ts", import.meta.url), {
      type: "module",
    });
    this.worker.onmessage = (event: MessageEvent<WorkerReply>) => {
      const reply = event.data;
      const waiter = this.pending.get(reply.requestId);
      if (!waiter) return;
      this.pending.delete(reply.requestId);
      if (reply.kind === "error") {
        waiter.reject(new Error(reply.message));
        return;
      }
      waiter.resolve({
        digest: this.lastDigest,
        sourceColor: reply.sourceColor,
        candidates: reply.candidates,
        palettes: reply.palettes,
        quantizedSize: reply.quantizedSize,
      });
    };
    return this.worker;
  }

  async fromFile(file: File): Promise<ImageThemeResult> {
    if (!/^image\/(jpeg|png|webp|gif)$/.test(file.type)) {
      throw new Error("请选择 JPG / PNG / WebP / GIF 图片");
    }
    if (file.size > 12 * 1024 * 1024) {
      throw new Error("图片不能超过 12MB");
    }
    const digest = await digestFile(file);
    this.lastDigest = digest;
    const cached = this.digestCache.get(digest);
    if (cached) return cached;

    const pixels = await decodeToPixels(file, 128);
    const worker = this.ensureWorker();
    const requestId = this.nextId++;
    const result = await new Promise<ImageThemeResult>((resolve, reject) => {
      this.pending.set(requestId, { resolve, reject });
      worker.postMessage({ kind: "extract", requestId, pixels });
    });
    result.digest = digest;
    this.digestCache.set(digest, result);
    this.colorCache.set(result.sourceColor, result.palettes);
    return result;
  }

  async fromColor(sourceColor: string, contrast = 0): Promise<{ light: ThemeColors; dark: ThemeColors }> {
    const key = `${sourceColor.toUpperCase()}:${contrast}`;
    const cached = this.colorCache.get(key);
    if (cached) return cached;
    const worker = this.ensureWorker();
    const requestId = this.nextId++;
    const result = await new Promise<ImageThemeResult>((resolve, reject) => {
      this.pending.set(requestId, { resolve, reject });
      worker.postMessage({ kind: "from-color", requestId, sourceColor, contrast });
    });
    this.colorCache.set(key, result.palettes);
    return result.palettes;
  }

  dispose(): void {
    this.worker?.terminate();
    this.worker = null;
    this.pending.clear();
  }
}

async function digestFile(file: File): Promise<string> {
  const buffer = await file.arrayBuffer();
  if (globalThis.crypto?.subtle) {
    const hash = await crypto.subtle.digest("SHA-256", buffer);
    return Array.from(new Uint8Array(hash))
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");
  }
  let hash = 0x811c9dc5;
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < bytes.length; i += 1) {
    hash ^= bytes[i];
    hash = Math.imul(hash, 0x01000193);
  }
  return `fnv_${(hash >>> 0).toString(16)}`;
}

async function decodeToPixels(file: File, maxSize: number): Promise<number[]> {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, maxSize / Math.max(bitmap.width, bitmap.height));
  const width = Math.max(1, Math.round(bitmap.width * scale));
  const height = Math.max(1, Math.round(bitmap.height * scale));
  const canvas =
    typeof OffscreenCanvas !== "undefined"
      ? new OffscreenCanvas(width, height)
      : Object.assign(document.createElement("canvas"), { width, height });
  const context = canvas.getContext("2d", { willReadFrequently: true }) as
    | CanvasRenderingContext2D
    | OffscreenCanvasRenderingContext2D
    | null;
  if (!context) throw new Error("浏览器无法读取图片");
  context.drawImage(bitmap, 0, 0, width, height);
  bitmap.close?.();
  const data = context.getImageData(0, 0, width, height).data;
  const pixels: number[] = [];
  for (let i = 0; i < data.length; i += 4) {
    if (data[i + 3] < 128) continue;
    pixels.push(((255 << 24) | (data[i] << 16) | (data[i + 1] << 8) | data[i + 2]) >>> 0);
  }
  return pixels;
}
