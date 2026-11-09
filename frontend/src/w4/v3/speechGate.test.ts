import { describe, expect, it } from "vitest";
import { SpeechGate } from "./speechGate";

describe("speech gating", () => {
  it("keeps idle silence local and includes bounded pre-roll when speech begins", () => {
    const gate = new SpeechGate();
    for (let index = 0; index < 100; index += 1) expect(gate.push(new Float32Array(640), 0).frames).toHaveLength(0);
    const result = gate.push(new Float32Array(640).fill(.2), .2);
    expect(result.frames).toHaveLength(6);
    expect(result.utteranceEnd).toBe(false);
  });
  it("ends on silence once and stops sending further idle frames", () => {
    const gate = new SpeechGate();
    gate.push(new Float32Array(640).fill(.2), .2);
    for (let index = 0; index < 19; index += 1) expect(gate.push(new Float32Array(640), 0).utteranceEnd).toBe(false);
    expect(gate.push(new Float32Array(640), 0).utteranceEnd).toBe(true);
    expect(gate.push(new Float32Array(640), 0).frames).toHaveLength(0);
  });
});
