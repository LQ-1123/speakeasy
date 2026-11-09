import { API_ORIGIN } from "../api";
import { clientApi, currentClientIdentity, nativeBridge, websocketUrl, type CallTicket } from "./clientApi";
import { OrderedPlayback } from "./orderedPlayback";
import { SpeechGate } from "./speechGate";

export type CallPhase = "idle" | "connecting" | "listening" | "responding" | "speaking" | "reconnecting" | "error";
export interface CallSnapshot {
  callId: string;
  friendId: string;
  friendName: string;
  phase: CallPhase;
  minimized: boolean;
  muted: boolean;
  speaker?: boolean;
  native?: boolean;
  userText: string;
  friendText: string;
  error?: string;
}
type Listener = (snapshot: CallSnapshot | null) => void;
type ServerEvent = { type: string; [key: string]: unknown };

export function pcm16le(samples: Float32Array): ArrayBuffer {
  const bytes = new ArrayBuffer(samples.length * 2);
  const view = new DataView(bytes);
  for (let i = 0; i < samples.length; i += 1) {
    const sample = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(i * 2, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
  }
  return bytes;
}

export function downsample(samples: Float32Array, inputRate: number, outputRate = 16_000): Float32Array {
  if (inputRate <= outputRate) return samples;
  const ratio = inputRate / outputRate;
  const output = new Float32Array(Math.floor(samples.length / ratio));
  for (let i = 0; i < output.length; i += 1) {
    const start = Math.floor(i * ratio);
    const end = Math.floor((i + 1) * ratio);
    let total = 0;
    for (let j = start; j < end; j += 1) total += samples[j];
    output[i] = total / Math.max(1, end - start);
  }
  return output;
}

class BrowserAudio {
  private stream?: MediaStream;
  private context?: AudioContext;
  private source?: MediaStreamAudioSourceNode;
  private worklet?: AudioWorkletNode;
  private currentSource?: AudioBufferSourceNode;
  private download?: AbortController;
  private playbackGeneration = 0;
  private workletUrl?: string;
  private cancelPlayback?: () => void;
  private stopped = false;

  async start(onFrame: (samples: Float32Array, rms: number) => void): Promise<void> {
    this.stopped = false;
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    if (this.stopped) { stream.getTracks().forEach((track) => track.stop()); throw new Error("Capture cancelled"); }
    this.stream = stream;
    this.context = new AudioContext({ sampleRate: 16_000 });
    const context = this.context;
    const script = `class PcmCapture extends AudioWorkletProcessor { process(inputs) { const input = inputs[0]?.[0]; if (input) this.port.postMessage(input.slice()); return true; } } registerProcessor('pcm-capture', PcmCapture);`;
    this.workletUrl = URL.createObjectURL(new Blob([script], { type: "text/javascript" }));
    await context.audioWorklet.addModule(this.workletUrl);
    if (this.stopped) throw new Error("Capture cancelled");
    this.source = context.createMediaStreamSource(this.stream);
    this.worklet = new AudioWorkletNode(context, "pcm-capture", { numberOfInputs: 1, numberOfOutputs: 0 });
    this.worklet.port.onmessage = (event: MessageEvent<Float32Array>) => {
      if (this.stopped) return;
      const frame = event.data;
      let power = 0;
      for (let i = 0; i < frame.length; i += 1) power += frame[i] * frame[i];
      onFrame(downsample(frame, this.context!.sampleRate), Math.sqrt(power / Math.max(1, frame.length)));
    };
    this.source.connect(this.worklet);
  }

  async play(url: string): Promise<void> {
    const generation = this.playbackGeneration;
    const context = this.context;
    if (!context) throw new Error("Audio is not ready");
    this.download = new AbortController();
    const timeout = window.setTimeout(() => this.download?.abort(), 15_000);
    let data: ArrayBuffer;
    try {
      const response = await fetch(url, { signal: this.download.signal });
      if (!response.ok) throw new Error("Audio download failed");
      data = await response.arrayBuffer();
    } finally { window.clearTimeout(timeout); }
    const buffer = await context.decodeAudioData(data);
    if (generation !== this.playbackGeneration) throw new Error("Playback interrupted");
    if (context.state === "suspended") await context.resume();
    await new Promise<void>((resolve, reject) => {
      const source = context.createBufferSource();
      source.buffer = buffer;
      source.connect(context.destination);
      this.currentSource = source;
      this.cancelPlayback = () => { source.onended = null; try { source.stop(); } catch { /* already stopped */ } source.disconnect(); reject(new Error("Playback interrupted")); };
      source.onended = () => {
        source.disconnect();
        if (this.currentSource === source) { this.currentSource = undefined; this.cancelPlayback = undefined; }
        resolve();
      };
      source.start();
    });
  }

  get isSpeaking(): boolean { return Boolean(this.currentSource); }
  stopPlayback(): void { this.playbackGeneration += 1; this.download?.abort(); this.cancelPlayback?.(); this.cancelPlayback = undefined; this.currentSource = undefined; }
  stop(): void {
    this.stopped = true;
    this.stopPlayback();
    this.worklet?.disconnect();
    this.source?.disconnect();
    this.stream?.getTracks().forEach((track) => track.stop());
    void this.context?.close();
    if (this.workletUrl) URL.revokeObjectURL(this.workletUrl);
    this.worklet = undefined;
    this.source = undefined;
    this.stream = undefined;
    this.context = undefined;
  }
}

export class CallSessionController {
  private snapshot: CallSnapshot | null = null;
  private listeners = new Set<Listener>();
  private socket?: WebSocket;
  private audio?: BrowserAudio;
  private playback?: OrderedPlayback<{ url: string; audioId: string; turnId: string }>;
  private reconnectUntil = 0;
  private sequence = 0;
  private turnId = "";
  private muted = false;
  private disposed = false;
  private bargeSamples = 0;
  private invalidTurns = new Set<string>();
  private heartbeat?: number;
  private turnFinished = false;
  private speechGate = new SpeechGate();
  private connectionGeneration = 0;
  private awaitingReply = false;
  private bargePreRoll: number[] = [];

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    listener(this.snapshot);
    return () => this.listeners.delete(listener);
  }

  getSnapshot = (): CallSnapshot | null => this.snapshot;

  private update(patch: Partial<CallSnapshot> | null): void {
    if (patch) this.snapshot = this.snapshot ? { ...this.snapshot, ...patch } : patch as CallSnapshot;
    for (const listener of this.listeners) listener(this.snapshot);
  }

  /** Preview-only snapshot. Never opens a socket or touches the call protocol. */
  startPreview(friend: { id: string; name: string }, phase: CallPhase = "listening"): void {
    this.disposed = false;
    this.snapshot = {
      callId: "preview-call",
      friendId: friend.id,
      friendName: friend.name,
      phase,
      minimized: false,
      muted: false,
      userText: "I tried that little coffee shop you mentioned. It was exactly as quiet as you said.",
      friendText: "That's the whole point of it. What did you order?",
      native: false,
    };
    this.update(this.snapshot);
  }

  async start(friend: { id: string; name: string }): Promise<void> {
    if (this.snapshot) return;
    if (
      typeof window !== "undefined" &&
      typeof window.location?.search === "string" &&
      new URLSearchParams(window.location.search).get("preview") === "1"
    ) {
      this.startPreview(friend, "connecting");
      window.setTimeout(() => {
        if (this.snapshot?.callId === "preview-call") this.update({ phase: "listening" });
      }, 900);
      return;
    }
    this.disposed = false;
    this.muted = false;
    this.bargeSamples = 0;
    this.reconnectUntil = 0;
    this.turnFinished = false;
    this.awaitingReply = false;
    this.bargePreRoll = [];
    window.clearInterval(this.heartbeat);
    this.sequence = 0;
    this.turnId = "";
    this.invalidTurns.clear();
    this.speechGate.reset();
    this.snapshot = { callId: "", friendId: friend.id, friendName: friend.name, phase: "connecting", minimized: false, muted: false, userText: "", friendText: "" };
    this.update(this.snapshot);
    try {
      const requestId = crypto.randomUUID();
      const ticket = await clientApi.createCall(friend.id, requestId);
      if (this.disposed || !this.snapshot) { await clientApi.endCall(ticket.call_id).catch(() => undefined); return; }
      const identity = await currentClientIdentity();
      if (this.disposed || !this.snapshot) { await clientApi.endCall(ticket.call_id).catch(() => undefined); return; }
      if (!identity) throw new Error("Sign in to place a call");
      this.update({ callId: ticket.call_id });
      const native = nativeBridge();
      let capabilities: { nativeCalls?: boolean } = {};
      try { capabilities = JSON.parse(native?.getCapabilities?.() || "{}"); } catch { /* optional bridge */ }
      if (capabilities.nativeCalls && native?.startCall) {
        this.update({ native: true, speaker: false });
        native.startCall(JSON.stringify({ apiOrigin: API_ORIGIN, callId: ticket.call_id, friendId: friend.id, ticket: ticket.ticket, wsPath: ticket.ws_path, friendName: friend.name, userId: identity.user_id, token: identity.token }));
        return;
      }
      await this.connect(ticket);
    } catch (error) {
      if (this.disposed) return;
      this.audio?.stop();
      window.clearInterval(this.heartbeat);
      this.disposed = true;
      this.socket?.close();
      if (this.snapshot?.callId) void clientApi.endCall(this.snapshot.callId).catch(() => undefined);
      this.update({ phase: "error", error: error instanceof Error ? error.message : "Could not start call" });
    }
  }

  private async connect(ticket: CallTicket): Promise<void> {
    const generation = ++this.connectionGeneration;
    this.update({ phase: "connecting" });
    const socket = new WebSocket(websocketUrl(ticket.ws_path));
    socket.binaryType = "arraybuffer";
    this.socket = socket;
    await new Promise<void>((resolve, reject) => {
      let authenticated = false;
      const remaining = this.reconnectUntil ? Math.max(1, this.reconnectUntil - Date.now()) : 12_000;
      const timeout = window.setTimeout(() => { reject(new Error("Connection timed out")); socket.close(); }, Math.min(12_000, remaining));
      socket.onmessage = (raw) => {
        if (generation !== this.connectionGeneration || this.disposed || typeof raw.data !== "string") return;
        try {
          const event = JSON.parse(raw.data) as ServerEvent;
          if (event.type === "ready") {
            authenticated = true;
            window.clearTimeout(timeout);
            this.reconnectUntil = 0;
            window.clearInterval(this.heartbeat);
            this.heartbeat = window.setInterval(() => this.send({ type: "ping" }), 10_000);
            this.send({ type: "mute", muted: this.muted });
            resolve();
          } else if (event.type === "error" && !authenticated) {
            window.clearTimeout(timeout);
            reject(new Error(String(event.message || "Call authentication failed")));
          }
          this.onServerEvent(event);
        } catch { /* malformed event */ }
      };
      socket.onclose = () => {
        window.clearTimeout(timeout);
        if (!authenticated) reject(new Error("Call authentication closed"));
        else if (generation === this.connectionGeneration && !this.disposed) void this.reconnect();
      };
      socket.onopen = () => {
        socket.send(JSON.stringify({ type: "auth", ticket: ticket.ticket, last_sequence: this.sequence || undefined }));
      };
      socket.onerror = () => { window.clearTimeout(timeout); reject(new Error("Could not connect")); };
    });
    if (generation !== this.connectionGeneration || this.disposed) { socket.close(); return; }
    this.playback = new OrderedPlayback(async ({ url, audioId, turnId }) => {
      if (turnId !== this.turnId) return;
      try {
        this.update({ phase: "speaking" });
        await this.audio!.play(url);
        if (turnId !== this.turnId || this.disposed) return;
        this.send({ type: "playback_done", turn_id: turnId, audio_id: audioId });
      } catch {
        if (turnId === this.turnId && !this.disposed) {
          this.send({ type: "playback_failed", turn_id: turnId, audio_id: audioId });
          this.invalidTurns.add(turnId);
          this.turnId = "";
          this.playback?.invalidate();
          this.awaitingReply = false;
          this.update({ phase: "error", error: "Audio could not play. Please speak again." });
        }
      }
    }, () => { if (this.turnFinished && !this.disposed) this.update({ phase: "listening" }); });
    this.audio = new BrowserAudio();
    const audio = this.audio;
    try {
      await audio.start((samples, rms) => { if (generation === this.connectionGeneration && !this.disposed) this.onPcm(samples, rms); });
    } catch (error) {
      audio.stop();
      if (generation !== this.connectionGeneration || this.disposed) return;
      throw error;
    }
    if (generation !== this.connectionGeneration || this.disposed) audio.stop();
  }

  private onPcm(samples: Float32Array, rms: number): void {
    if (this.muted || !this.socket || this.socket.readyState !== WebSocket.OPEN) return;
    if (this.awaitingReply || this.audio?.isSpeaking || this.snapshot?.phase === "responding") {
      this.bargePreRoll.push(...samples);
      if (this.bargePreRoll.length > 3200) this.bargePreRoll.splice(0, this.bargePreRoll.length - 3200);
      if (rms > 0.055) {
        this.bargeSamples += samples.length;
        if (this.bargeSamples < 1600) return;
        this.audio?.stopPlayback();
        this.playback?.invalidate();
        this.send({ type: "interrupt", turn_id: this.turnId });
        this.invalidTurns.add(this.turnId);
        this.turnId = "";
        this.awaitingReply = false;
        this.speechGate.reset();
        samples = new Float32Array(this.bargePreRoll);
        this.bargePreRoll = [];
        this.bargeSamples = 0;
        this.update({ phase: "listening" });
      } else { this.bargeSamples = 0; return; }
    }
    const gated = this.speechGate.push(samples, rms);
    for (const frame of gated.frames) this.socket.send(pcm16le(frame));
    if (gated.utteranceEnd) {
      this.awaitingReply = true;
      this.turnId = crypto.randomUUID();
      this.send({ type: "utterance_end", turn_id: this.turnId });
      this.update({ phase: "responding" });
    }
  }

  private onServerEvent(event: ServerEvent): void {
    if (typeof event.sequence === "number") this.sequence = Math.max(this.sequence, event.sequence);
    const eventTurn = typeof event.turn_id === "string" ? event.turn_id : "";
    if (eventTurn && (this.invalidTurns.has(eventTurn) || this.turnId && this.turnId !== eventTurn)) return;
    switch (event.type) {
      case "ready": this.update({ phase: "listening" }); break;
      case "transcript": this.update({ userText: String(event.text ?? "") }); break;
      case "responding": {
        const turnId = String(event.turn_id ?? "");
        if (turnId !== this.turnId) { this.audio?.stopPlayback(); this.playback?.invalidate(); }
        this.turnId = turnId;
        this.turnFinished = false;
        this.update({ phase: "responding" });
        break;
      }
      case "audio": {
        const eventTurn = String(event.turn_id ?? this.turnId);
        if (this.invalidTurns.has(eventTurn)) return;
        this.turnId = eventTurn;
        const rawUrl = String(event.audio_url ?? "");
        const url = rawUrl ? new URL(rawUrl, API_ORIGIN || window.location.origin).toString() : "";
        const index = Number(event.index ?? 0);
        const audioId = String(event.audio_id ?? "");
        if (url && this.turnId) {
          this.update({ phase: "speaking", friendText: String(event.text ?? "") });
          this.playback?.enqueue(index, { url, audioId, turnId: this.turnId });
        }
        break;
      }
      case "turn_done": this.turnFinished = true; this.awaitingReply = false; if (!this.playback?.isBusy) this.update({ phase: "listening" }); break;
      case "interrupted": this.audio?.stopPlayback(); this.playback?.invalidate(); this.awaitingReply = false; this.update({ phase: "listening" }); break;
      case "error": {
        if (this.turnId) this.invalidTurns.add(this.turnId);
        this.turnId = "";
        this.awaitingReply = false;
        this.bargePreRoll = [];
        this.bargeSamples = 0;
        this.speechGate.reset();
        this.audio?.stopPlayback();
        this.playback?.invalidate();
        this.update({ phase: "error", error: String(event.message ?? "通话出现问题，请重试。") });
        break;
      }
      case "ended": void this.finish(false); break;
    }
  }

  private send(data: Record<string, unknown>): void {
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(data));
  }

  private async reconnect(): Promise<void> {
    if (this.disposed || !this.snapshot?.callId) return;
    this.audio?.stop();
    if (this.turnId) this.invalidTurns.add(this.turnId);
    this.turnId = "";
    this.awaitingReply = false;
    this.bargePreRoll = [];
    this.speechGate.reset();
    window.clearInterval(this.heartbeat);
    this.playback?.invalidate();
    this.reconnectUntil ||= Date.now() + 20_000;
    if (Date.now() >= this.reconnectUntil) {
      this.update({ phase: "error", error: "Connection lost" });
      return;
    }
    this.update({ phase: "reconnecting" });
    try {
      const remaining = Math.max(1, this.reconnectUntil - Date.now());
      const ticket = await clientApi.refreshCallTicket(this.snapshot.callId, AbortSignal.timeout(remaining));
      if (this.disposed) return;
      if (Date.now() >= this.reconnectUntil) { this.update({ phase: "error", error: "Connection lost" }); return; }
      await this.connect(ticket);
    } catch {
      window.setTimeout(() => void this.reconnect(), 1200);
    }
  }

  minimize(): void { this.update({ minimized: true }); }
  expand(): void { this.update({ minimized: false }); }

  setMuted(muted: boolean): void {
    this.muted = muted;
    this.speechGate.reset();
    this.update({ muted });
    const native = nativeBridge();
    if (native?.callAction && this.snapshot?.callId) native.callAction(JSON.stringify({ type: "mute", muted, callId: this.snapshot.callId }));
    else this.send({ type: "mute", muted });
  }

  setSpeaker(speaker: boolean): void {
    if (!this.snapshot?.native) return;
    this.update({ speaker });
    nativeBridge()?.callAction?.(JSON.stringify({ type: "speaker", speaker, callId: this.snapshot.callId }));
  }

  async end(): Promise<void> { await this.finish(true); }

  private async finish(notifyServer: boolean): Promise<void> {
    const callId = this.snapshot?.callId;
    this.disposed = true;
    this.connectionGeneration += 1;
    this.socket?.close();
    this.socket = undefined;
    window.clearInterval(this.heartbeat);
    this.audio?.stop();
    this.audio = undefined;
    this.playback?.invalidate();
    this.playback = undefined;
    this.snapshot = null;
    this.update(null);
    if (notifyServer && callId) {
      const native = nativeBridge();
      native?.callAction?.(JSON.stringify({ type: "end", callId }));
      await clientApi.endCall(callId).catch(() => undefined);
    }
  }

  handleNativeEvent(event: CustomEvent<{ type?: string; event?: ServerEvent; state?: Record<string, unknown> }>): void {
    const detail = event.detail;
    if (detail.type === "call" && detail.state) {
      const state = detail.state;
      const nativePhase = String(state.state ?? state.phase ?? "");
      if (nativePhase === "idle" || nativePhase === "ended") { this.snapshot = null; this.update(null); return; }
      const callId = String(state.callId ?? state.call_id ?? "");
      if (!this.snapshot && callId) this.snapshot = { callId, friendId: String(state.friendId ?? state.friend_id ?? ""), friendName: String(state.friendName ?? state.friend_name ?? ""), phase: "listening", minimized: true, muted: Boolean(state.muted), native: true, speaker: Boolean(state.speaker), userText: "", friendText: "" };
      const phase: CallPhase = nativePhase === "active" || nativePhase === "connected" ? this.snapshot?.phase === "connecting" || this.snapshot?.phase === "reconnecting" ? "listening" : this.snapshot?.phase ?? "listening" : nativePhase === "connecting" || nativePhase === "reconnecting" || nativePhase === "error" ? nativePhase : this.snapshot?.phase ?? "listening";
      if (this.snapshot) this.update({ phase, muted: Boolean(state.muted), speaker: state.speaker === undefined ? this.snapshot.speaker : Boolean(state.speaker), error: state.message ? String(state.message) : undefined });
    }
    if (detail.type === "call" && detail.event) this.onServerEvent(detail.event);
  }
}

export const callSession = new CallSessionController();
