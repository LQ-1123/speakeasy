"use client";

import { useEffect } from "react";

export default function LegacyRedirect() {
  useEffect(() => { window.location.replace(`/${window.location.hash || "#/chats"}`); }, []);
  return <main style={{ padding: 24, fontFamily: "system-ui" }}>正在打开 Sonder…</main>;
}
