(async () => {
  const t0 = performance.now();
  const res = await fetch("/api/v3/feed?limit=12", { cache: "no-store" });
  const data = await res.json();
  const posts = (data.items || []).filter((it) => it.kind === "persona_post").slice(0, 3);
  const results = [];
  for (const p of posts) {
    const url = `/api/v3/persona-posts/${p.id}/speech?v=${p.content_hash}`;
    const audio = new Audio();
    audio.preload = "auto";
    const started = performance.now();
    audio.src = url;
    const ok = await new Promise((resolve) => {
      const done = (v) => resolve(v);
      audio.addEventListener("canplay", () => done(true), { once: true });
      audio.addEventListener("error", () => done(false), { once: true });
      setTimeout(() => done(null), 6000);
    });
    const ms = performance.now() - started;
    // 第二次（浏览器已缓存）模拟点击播放
    const click = performance.now();
    const a2 = new Audio(url);
    const played = await new Promise((resolve) => {
      a2.addEventListener("playing", () => resolve(true), { once: true });
      a2.addEventListener("error", () => resolve(false), { once: true });
      setTimeout(() => resolve(false), 6000);
    });
    const clickMs = performance.now() - click;
    a2.pause();
    results.push({ id: p.id, canplay_ms: Math.round(ms), click_to_playing_ms: Math.round(clickMs), played });
  }
  return JSON.stringify({ total_ms: Math.round(performance.now() - t0), results });
})();
