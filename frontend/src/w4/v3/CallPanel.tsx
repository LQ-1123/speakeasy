"use client";

import { useEffect, useRef, useSyncExternalStore } from "react";
import { ChevronDown, ChevronUp, Mic, MicOff, PhoneOff, Volume2, VolumeX } from "lucide-react";
import { useShallow } from "zustand/react/shallow";

import { useApp } from "../store";
import { Avatar } from "../ui";
import { haptic } from "../../theme/haptics";
import { callSession, type CallPhase } from "./callSession";
import "../styles/call.css";

const PHASE_TEXT: Record<CallPhase, string> = {
  idle: "",
  connecting: "正在接通",
  listening: "正在聆听",
  responding: "正在回应",
  speaking: "对方正在说话",
  reconnecting: "正在重新连接",
  error: "通话中断",
};

function useCall() {
  return useSyncExternalStore(
    (listener) => callSession.subscribe(() => listener()),
    callSession.getSnapshot,
    () => null,
  );
}

function CallDock() {
  const call = useCall();
  const friend = useApp((state) =>
    call?.friendId ? state.friends.find((f) => f.id === call.friendId) : undefined,
  );
  if (!call?.minimized) return null;
  return (
    <div className="se-call-dock" role="region" aria-label="缩小通话">
      <button
        className="se-call-dock-main"
        onClick={() => {
          haptic("selection");
          callSession.expand();
        }}
      >
        <span className="se-call-dock-live" aria-hidden>
          <i />
        </span>
        <Avatar friend={friend} name={call.friendName} size={38} />
        <span className="se-call-dock-text">
          <strong>{call.friendName}</strong>
          <small>{PHASE_TEXT[call.phase]}</small>
        </span>
        <span className="se-call-dock-return">
          返回通话 <ChevronUp size={15} />
        </span>
      </button>
      <button
        className="se-call-dock-end"
        aria-label="结束通话"
        onClick={() => {
          haptic("error");
          void callSession.end();
        }}
      >
        <PhoneOff size={18} />
      </button>
    </div>
  );
}

function CallOverlay() {
  const call = useCall();
  const { profile } = useApp(useShallow((state) => ({ profile: state.profile })));
  const friend = useApp((state) =>
    call?.friendId ? state.friends.find((f) => f.id === call.friendId) : undefined,
  );
  const captionRef = useRef<HTMLDivElement>(null);
  const captionCount = call?.captions.length ?? 0;
  useEffect(() => {
    const node = captionRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [captionCount]);
  if (!call || call.minimized) return null;

  const speaking = call.phase === "speaking" || call.phase === "responding";
  return (
    <section
      className={`se-call-panel ${speaking ? "is-active" : ""}`}
      aria-label="语音通话"
      role="dialog"
      aria-modal="true"
    >
      <header>
        <button
          aria-label="收起通话"
          onClick={() => {
            haptic("selection");
            callSession.minimize();
          }}
        >
          <ChevronDown />
        </button>
        <span>语音通话</span>
        <span className="se-call-panel-spacer" />
      </header>

      <div className="se-call-ambient" aria-hidden>
        <i />
        <i />
      </div>

      <main>
        <div className={`se-call-portrait ${speaking ? "is-speaking" : ""}`}>
          <div className="se-call-rings" aria-hidden>
            <i />
            <i />
            <i />
          </div>
          <div className="se-call-avatar">
            <Avatar friend={friend} name={call.friendName} size={136} round />
          </div>
        </div>
        <h1>{call.friendName}</h1>
        <p className="se-call-status" aria-live="polite">
          <span className={`se-call-status-dot ${call.phase}`} aria-hidden />
          {PHASE_TEXT[call.phase]}
        </p>

        <div className="se-call-captions" ref={captionRef} aria-live="polite">
          {call.captions.length ? (
            call.captions.map((caption) => (
              <p key={caption.key} className={caption.role === "me" ? "is-me" : undefined}>
                <small>{caption.role === "me" ? profile.name || "我" : call.friendName}</small>
                {caption.text}
              </p>
            ))
          ) : (
            <p className="is-hint">开始说话，字幕会出现在这里</p>
          )}
        </div>
        {call.error ? (
          <p role="alert" className="se-call-error">
            {call.error}
          </p>
        ) : null}
      </main>

      <footer>
        <button
          className="se-call-control"
          onClick={() => {
            haptic("toggle");
            callSession.setMuted(!call.muted);
          }}
          aria-pressed={call.muted}
          aria-label={call.muted ? "取消静音" : "静音"}
        >
          {call.muted ? <MicOff /> : <Mic />}
          <span>{call.muted ? "取消静音" : "静音"}</span>
        </button>
        <button
          className="se-call-control se-end-call"
          onClick={() => {
            haptic("error");
            void callSession.end();
          }}
          aria-label="结束通话"
        >
          <PhoneOff />
          <span>挂断</span>
        </button>
        {call.native ? (
          <button
            className="se-call-control"
            onClick={() => {
              haptic("toggle");
              callSession.setSpeaker(!call.speaker);
            }}
            aria-pressed={call.speaker}
            aria-label={call.speaker ? "关闭扬声器" : "打开扬声器"}
          >
            {call.speaker ? <Volume2 /> : <VolumeX />}
            <span>{call.speaker ? "扬声器已开" : "扬声器"}</span>
          </button>
        ) : (
          <button
            className="se-call-control"
            onClick={() => {
              haptic("toggle");
              callSession.setSpeaker(!call.speaker);
            }}
            aria-pressed={call.speaker}
            aria-label={call.speaker ? "关闭扬声器" : "打开扬声器"}
            title="浏览器通话沿用系统输出设备"
          >
            {call.speaker ? <Volume2 /> : <VolumeX />}
            <span>{call.speaker ? "扬声器已开" : "扬声器"}</span>
          </button>
        )}
      </footer>
    </section>
  );
}

/**
 * CallDock is an in-flow layout track (never covers the header or composer).
 * CallOverlay is the full-screen call surface.
 */
export function CallPanel() {
  return (
    <>
      <CallDock />
      <CallOverlay />
    </>
  );
}
