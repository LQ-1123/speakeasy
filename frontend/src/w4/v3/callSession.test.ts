import { afterEach, describe, expect, it, vi } from "vitest";
import { CallSessionController, downsample, pcm16le } from "./callSession";
import { clientApi } from "./clientApi";

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("browser PCM transport", () => {
  it("clips and encodes signed PCM16 in little endian", () => {
    const data = new DataView(pcm16le(new Float32Array([-2, -1, 0, 1, 2])));
    expect([0, 2, 4, 6, 8].map((offset) => data.getInt16(offset, true))).toEqual([-32768, -32768, 0, 32767, 32767]);
  });
  it("resamples 48kHz mono input to 16kHz", () => {
    expect(Array.from(downsample(new Float32Array([1, 1, 1, 0, 0, 0]), 48000))).toEqual([1, 0]);
  });
});

describe("native session ownership", () => {
  it("keeps the call alive after a view unsubscribes and dispatches server detail.event", async () => {
    const startCall = vi.fn();
    vi.stubGlobal("window", { clearInterval, SpeakEasyNative: {
      getCapabilities: () => JSON.stringify({ nativeCalls: true, identityMigration: "ready" }),
      getIdentity: () => JSON.stringify({ user_id: "owner", token: "secret" }),
      startCall,
    } });
    vi.spyOn(clientApi, "createCall").mockResolvedValue({ call_id: "call1", ticket: "single-use", ws_path: "/stream", expires_in: 60 });
    const controller = new CallSessionController();
    const unsubscribe = controller.subscribe(() => undefined);
    await controller.start({ id: "friend1", name: "Callum" });
    unsubscribe();
    controller.minimize();
    expect(controller.getSnapshot()?.callId).toBe("call1");
    expect(controller.getSnapshot()?.minimized).toBe(true);
    expect(JSON.parse(startCall.mock.calls[0][0])).toMatchObject({ callId: "call1", userId: "owner", ticket: "single-use" });
    controller.handleNativeEvent({ detail: { type: "call", event: { type: "transcript", text: "hello" }, state: { state: "connected", callId: "call1" } } } as never);
    expect(controller.getSnapshot()?.userText).toBe("hello");
  });

  it("restores lifecycle snapshots from camelCase native state", () => {
    const controller = new CallSessionController();
    controller.handleNativeEvent({ detail: { type: "call", state: { state: "connected", callId: "existing", friendName: "Giulia", muted: true } } } as never);
    expect(controller.getSnapshot()).toMatchObject({ callId: "existing", friendName: "Giulia", muted: true, minimized: true });
    controller.handleNativeEvent({ detail: { type: "call", state: { state: "idle" } } } as never);
    expect(controller.getSnapshot()).toBeNull();
  });

  it("keeps the full transcript instead of only the last two lines", () => {
    vi.stubGlobal("window", { location: { origin: "https://fixture.test" } });
    const controller = new CallSessionController();
    controller.startPreview({ id: "friend1", name: "Chloe" });
    const event = (payload: Record<string, unknown>) =>
      ({ detail: { type: "call", event: payload } }) as never;

    controller.handleNativeEvent(event({ type: "transcript", text: "partial", final: false, turn_id: "t1" }));
    controller.handleNativeEvent(event({ type: "transcript", text: "first line", final: true, turn_id: "t1" }));
    controller.handleNativeEvent(event({ type: "transcript", text: "second line", final: true, turn_id: "t2" }));
    for (const [index, text] of ["reply one", "reply two", "reply three"].entries()) {
      controller.handleNativeEvent(event({
        type: "audio", turn_id: "t2", audio_url: `/audio/${index}.wav`,
        audio_id: `a${index}`, text, index,
      }));
    }

    expect(controller.getSnapshot()?.captions.map((item) => [item.role, item.text])).toEqual([
      ["me", "I tried that little coffee shop you mentioned. It was exactly as quiet as you said."],
      ["friend", "That's the whole point of it. What did you order?"],
      ["me", "first line"],
      ["me", "second line"],
      ["friend", "reply one"],
      ["friend", "reply two"],
      ["friend", "reply three"],
    ]);
    expect(controller.getSnapshot()?.captions.some((item) => item.text === "partial")).toBe(false);
  });
});

describe("browser authentication handshake", () => {
  class Socket {
    static OPEN = 1;
    static instances: Socket[] = [];
    readyState = 0;
    onopen?: () => void;
    onclose?: () => void;
    onmessage?: (event: { data: string }) => void;
    sent: Array<string | ArrayBuffer> = [];
    constructor(readonly url: string) { Socket.instances.push(this); }
    send(value: string | ArrayBuffer) { this.sent.push(value); }
    close() { this.readyState = 3; this.onclose?.(); }
    open() { this.readyState = 1; this.onopen?.(); }
    event(type: string) { this.onmessage?.({ data: JSON.stringify({ type }) }); }
  }

  async function setup() {
    Socket.instances = [];
    vi.stubGlobal("WebSocket", Socket);
    vi.stubGlobal("window", {
      setTimeout, clearTimeout, setInterval, clearInterval,
      location: { origin: "https://fixture.test" },
      SpeakEasyNative: { getCapabilities: () => JSON.stringify({ nativeCalls: false }), getIdentity: () => JSON.stringify({ user_id: "owner", token: "secret" }) },
    });
    vi.spyOn(clientApi, "createCall").mockResolvedValue({ call_id: "web-call", ticket: "short-lived-ticket", ws_path: "/stream", expires_in: 60 });
    vi.spyOn(clientApi, "endCall").mockResolvedValue(undefined);
  }

  it("does not request microphone permission before server ready", async () => {
    await setup();
    const getUserMedia = vi.fn();
    vi.stubGlobal("navigator", { mediaDevices: { getUserMedia } });
    const controller = new CallSessionController();
    const started = controller.start({ id: "friend", name: "Callum" });
    await vi.waitFor(() => expect(Socket.instances).toHaveLength(1));
    const socket = Socket.instances[0];
    socket.open();
    await Promise.resolve();
    expect(getUserMedia).not.toHaveBeenCalled();
    expect(socket.url).not.toContain("ticket");
    expect(JSON.parse(socket.sent[0] as string)).toEqual({ type: "auth", ticket: "short-lived-ticket" });
    socket.close();
    await started;
    expect(controller.getSnapshot()?.phase).toBe("error");
    expect(getUserMedia).not.toHaveBeenCalled();
  });

  it("stops a delayed microphone stream after the call was ended", async () => {
    await setup();
    let resolveMedia: (stream: unknown) => void = () => undefined;
    const getUserMedia = vi.fn(() => new Promise((resolve) => { resolveMedia = resolve; }));
    vi.stubGlobal("navigator", { mediaDevices: { getUserMedia } });
    const controller = new CallSessionController();
    const started = controller.start({ id: "friend", name: "Callum" });
    await vi.waitFor(() => expect(Socket.instances).toHaveLength(1));
    Socket.instances[0].open();
    Socket.instances[0].event("ready");
    await vi.waitFor(() => expect(getUserMedia).toHaveBeenCalledOnce());
    await controller.end();
    const stop = vi.fn();
    resolveMedia({ getTracks: () => [{ stop }] });
    await started;
    expect(stop).toHaveBeenCalledOnce();
    expect(controller.getSnapshot()).toBeNull();
  });

  it("captures speech again when a new call starts after ending a muted call", async () => {
    await setup();
    class Worklet {
      static instances: Worklet[] = [];
      port: { onmessage?: (event: { data: Float32Array }) => void } = {};
      constructor() { Worklet.instances.push(this); }
      disconnect() {}
    }
    vi.stubGlobal("AudioWorkletNode", Worklet);
    vi.stubGlobal("AudioContext", class {
      sampleRate = 16_000;
      state = "running";
      audioWorklet = { addModule: async () => undefined };
      createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
      close() { return Promise.resolve(); }
    });
    vi.stubGlobal("navigator", { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }) } });
    const controller = new CallSessionController();
    const first = controller.start({ id: "friend", name: "Callum" });
    await vi.waitFor(() => expect(Socket.instances).toHaveLength(1));
    Socket.instances[0].open(); Socket.instances[0].event("ready");
    await first;
    controller.setMuted(true);
    await controller.end();
    const second = controller.start({ id: "friend", name: "Callum" });
    await vi.waitFor(() => expect(Socket.instances).toHaveLength(2));
    Socket.instances[1].open(); Socket.instances[1].event("ready");
    await second;
    Worklet.instances.at(-1)!.port.onmessage!({ data: new Float32Array(640).fill(.2) });
    expect(controller.getSnapshot()?.muted).toBe(false);
    expect(Socket.instances[1].sent.some((value) => value instanceof ArrayBuffer)).toBe(true);
    expect(Socket.instances[1].sent.filter((value) => typeof value === "string").map((value) => JSON.parse(value))).toContainEqual({ type: "mute", muted: false });
    const lastWorklet = Worklet.instances.at(-1)!;
    for (let index = 0; index < 20; index += 1) lastWorklet.port.onmessage!({ data: new Float32Array(640) });
    const pending = Socket.instances[1].sent.filter((value) => typeof value === "string").map((value) => JSON.parse(value)).find((event) => event.type === "utterance_end");
    expect(pending.turn_id).toBeTruthy();
    for (let index = 0; index < 3; index += 1) lastWorklet.port.onmessage!({ data: new Float32Array(640).fill(.2) });
    const controls = Socket.instances[1].sent.filter((value) => typeof value === "string").map((value) => JSON.parse(value));
    expect(controls).toContainEqual({ type: "interrupt", turn_id: pending.turn_id });
    Socket.instances[1].onmessage!({ data: JSON.stringify({ type: "transcript", turn_id: pending.turn_id, text: "stale transcript" }) });
    Socket.instances[1].onmessage!({ data: JSON.stringify({ type: "responding", turn_id: pending.turn_id }) });
    expect(controller.getSnapshot()?.userText).not.toBe("stale transcript");
    expect(controller.getSnapshot()?.phase).toBe("listening");
    await controller.end();
  });
});
