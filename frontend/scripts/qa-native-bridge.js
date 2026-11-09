// Run only in the offline QA fixture browser through agent-browser eval.
window.SpeakEasyNative = {
  getCapabilities: () => JSON.stringify({ version: 1, nativeCalls: true, dynamicColor: false, identityMigration: "ready" }),
  getIdentity: () => JSON.stringify({ user_id: "fixture-user", token: "fixture-token", device_key: "fixture-device-key" }),
  getTheme: () => JSON.stringify({ dark: false, insets: { top: 24, bottom: 24, ime: 0 } }),
  getCallState: () => JSON.stringify(window.__fixtureCallState || { state: "idle" }),
  startCall: (json) => {
    const call = JSON.parse(json);
    window.__fixtureCallState = { state: "connected", callId: call.callId, friendId: call.friendId, friendName: call.friendName };
    window.dispatchEvent(new CustomEvent("speakeasy:native", { detail: { type: "call", state: window.__fixtureCallState, event: { type: "ready" } } }));
  },
  callAction: (json) => {
    const action = JSON.parse(json);
    if (action.type === "end") window.__fixtureCallState = { state: "ended" };
    if (action.type === "mute") window.__fixtureCallState.muted = action.muted;
    window.dispatchEvent(new CustomEvent("speakeasy:native", { detail: { type: "call", state: window.__fixtureCallState } }));
  },
};
window.dispatchEvent(new CustomEvent("speakeasy:native", { detail: { type: "theme" } }));
