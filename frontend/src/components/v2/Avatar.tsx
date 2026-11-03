"use client";

/** 确定性圆形头像：avatar_seed 派生渐变 + 首字母 + 国旗角标。 */

import { avatarColors, flagOf, initialsOf } from "@/lib/v2/format";
import type { Friend } from "@/lib/v2/types";

interface AvatarProps {
  name: string;
  seed: string;
  friend?: Pick<Friend, "nationality" | "native_lang">;
  size?: number;
  flag?: boolean;
  ring?: boolean;
}

export function Avatar({ name, seed, friend, size = 46, flag = true, ring = false }: AvatarProps) {
  const { from, to } = avatarColors(seed || name);
  const fontSize = Math.max(11, Math.round(size * 0.36));
  const badge = friend ? flagOf(friend) : "";

  return (
    <div className="relative shrink-0" style={{ width: size, height: size }}>
      <div
        aria-hidden
        className={`flex h-full w-full items-center justify-center rounded-full font-semibold text-white ${
          ring ? "ring-2 ring-white" : ""
        }`}
        style={{
          background: `linear-gradient(135deg, ${from} 0%, ${to} 100%)`,
          fontSize,
          letterSpacing: "0.02em",
          boxShadow: "inset 0 -1px 0 rgba(0,0,0,0.08)",
        }}
      >
        {initialsOf(name)}
      </div>
      {badge ? (
        <span
          className="absolute -bottom-0.5 -right-0.5 flex items-center justify-center rounded-full bg-white shadow-sm"
          style={{ width: Math.round(size * 0.42), height: Math.round(size * 0.42), fontSize: Math.round(size * 0.24) }}
          title={friend?.nationality || friend?.native_lang || ""}
        >
          {badge}
        </span>
      ) : null}
    </div>
  );
}
