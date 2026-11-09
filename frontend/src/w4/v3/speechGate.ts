/** 16kHz mono speech gate: 200ms pre-roll, 800ms trailing silence, 28s maximum. */
export class SpeechGate {
  private preRoll: number[] = [];
  private pending: number[] = [];
  private speaking = false;
  private silenceSamples = 0;
  private utteranceSamples = 0;

  reset(): void { this.preRoll = []; this.pending = []; this.speaking = false; this.silenceSamples = 0; this.utteranceSamples = 0; }

  push(samples: Float32Array, rms: number): { frames: Float32Array[]; utteranceEnd: boolean } {
    const frames: Float32Array[] = [];
    if (!this.speaking && rms < .018) {
      this.preRoll.push(...samples);
      if (this.preRoll.length > 3200) this.preRoll.splice(0, this.preRoll.length - 3200);
      return { frames, utteranceEnd: false };
    }
    if (!this.speaking) {
      this.speaking = true;
      this.pending = this.preRoll;
      this.utteranceSamples = this.preRoll.length;
      this.preRoll = [];
    }
    this.pending.push(...samples);
    this.utteranceSamples += samples.length;
    this.silenceSamples = rms < .018 ? this.silenceSamples + samples.length : 0;
    while (this.pending.length >= 640) frames.push(new Float32Array(this.pending.splice(0, 640)));
    const utteranceEnd = this.silenceSamples >= 12_800 || this.utteranceSamples >= 448_000;
    if (utteranceEnd) {
      if (this.pending.length) {
        const final = new Float32Array(Math.max(320, this.pending.length));
        final.set(this.pending);
        frames.push(final);
      }
      this.reset();
    }
    return { frames, utteranceEnd };
  }
}
