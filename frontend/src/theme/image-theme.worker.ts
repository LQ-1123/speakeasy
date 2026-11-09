/// <reference lib="webworker" />

import {
  QuantizerCelebi,
  Score,
  hexFromArgb,
} from "@material/material-color-utilities";

import { palettesFromSource } from "./scheme";

interface ExtractRequest {
  kind: "extract";
  requestId: number;
  pixels: number[];
}

interface ColorRequest {
  kind: "from-color";
  requestId: number;
  sourceColor: string;
  contrast?: number;
}

type Request = ExtractRequest | ColorRequest;

self.onmessage = (event: MessageEvent<Request>) => {
  const data = event.data;
  try {
    if (data.kind === "extract") {
      const quantized = QuantizerCelebi.quantize(data.pixels, 128);
      const ranked = Score.score(quantized, { desired: 6, filter: true });
      const candidates = ranked.map((argb) => hexFromArgb(argb));
      const sourceColor = candidates[0] ?? "#4C5E8B";
      const palettes = palettesFromSource(sourceColor, 0);
      self.postMessage({
        kind: "result",
        requestId: data.requestId,
        sourceColor,
        candidates,
        palettes,
        quantizedSize: quantized.size,
      });
      return;
    }
    const palettes = palettesFromSource(data.sourceColor, data.contrast ?? 0);
    self.postMessage({
      kind: "result",
      requestId: data.requestId,
      sourceColor: data.sourceColor,
      candidates: [data.sourceColor],
      palettes,
      quantizedSize: 0,
    });
  } catch (error) {
    self.postMessage({
      kind: "error",
      requestId: data.requestId,
      message: error instanceof Error ? error.message : "theme generation failed",
    });
  }
};

export {};
