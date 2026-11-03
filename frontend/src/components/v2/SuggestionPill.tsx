"use client";

/** 用户气泡下方的「✨ 地道表达」胶囊：展开解释 / 试听 / 收藏。 */

import { useEffect, useRef, useState } from "react";
import { Bookmark, BookmarkCheck, ChevronDown, Loader2, Sparkles, Volume2 } from "lucide-react";

import { useToast } from "@/components/v2/Toast";
import { api } from "@/lib/v2/api";
import { errorText } from "@/lib/v2/format";
import type { Friend, Message, Suggestion } from "@/lib/v2/types";

export function SuggestionPill({
  suggestion,
  message,
  friend,
  saved: savedProp,
}: {
  suggestion: Suggestion;
  message: Message;
  friend: Friend;
  saved?: boolean;
}) {
  const { showToast } = useToast();
  const [open, setOpen] = useState(false);
  const [saved, setSaved] = useState(savedProp ?? suggestion.is_saved);
  const [saving, setSaving] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  useEffect(() => {
    setSaved(savedProp ?? suggestion.is_saved);
  }, [savedProp, suggestion.is_saved]);

  useEffect(() => {
    return () => {
      const audio = audioRef.current;
      if (audio) {
        audio.pause();
        audio.src = "";
      }
      audioRef.current = null;
    };
  }, []);

  const speak = () => {
    if (speaking) {
      audioRef.current?.pause();
      setSpeaking(false);
      return;
    }
    const url = api.ttsSample(suggestion.better_version, friend.voice_id);
    const audio = new Audio(url);
    audioRef.current = audio;
    audio.onended = () => setSpeaking(false);
    audio.onerror = () => {
      setSpeaking(false);
      showToast("试听失败（TTS 暂不可用）", "error");
    };
    void audio
      .play()
      .then(() => setSpeaking(true))
      .catch(() => {
        setSpeaking(false);
        showToast("试听失败（TTS 暂不可用）", "error");
      });
  };

  const save = async () => {
    if (saved || saving) return;
    setSaving(true);
    try {
      await api.createExpression({
        original_text: message.transcript || suggestion.better_version,
        better_expression: suggestion.better_version,
        explanation: suggestion.explanation,
        source_friend_id: friend.id,
      });
      setSaved(true);
      showToast("已收藏到表达本 ⭐", "success");
    } catch (err) {
      showToast(`收藏失败：${errorText(err)}`, "error");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="se-suggestion">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className={`se-suggestion-pill ${open ? "open" : ""}`}
        aria-expanded={open}
      >
        <Sparkles size={12} className="shrink-0" />
        <span className="truncate">地道表达 · {suggestion.better_version}</span>
        <ChevronDown size={12} className={`shrink-0 transition-transform ${open ? "rotate-180" : ""}`} />
      </button>

      {open ? (
        <div className="se-suggestion-card">
          <p className="se-suggestion-better">{suggestion.better_version}</p>
          {suggestion.explanation ? (
            <p className="se-suggestion-explain">{suggestion.explanation}</p>
          ) : null}
          {suggestion.tag ? <span className="se-suggestion-tag">{suggestion.tag}</span> : null}

          <div className="mt-2 flex items-center gap-2">
            <button type="button" onClick={speak} className="se-suggestion-action">
              <Volume2 size={12} />
              {speaking ? "停止" : "试听"}
            </button>
            <button
              type="button"
              onClick={() => void save()}
              disabled={saved || saving}
              className={`se-suggestion-action ${saved ? "saved" : ""}`}
            >
              {saving ? (
                <Loader2 size={12} className="animate-spin" />
              ) : saved ? (
                <BookmarkCheck size={12} />
              ) : (
                <Bookmark size={12} />
              )}
              {saved ? "已收藏" : "收藏"}
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
