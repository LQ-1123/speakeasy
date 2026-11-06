(async () => {
  const mk = async (content, visibility) => {
    const r = await fetch("/api/v3/posts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, visibility }),
    });
    const d = await r.json();
    return { status: r.status, id: d.post && d.post.id, visibility: d.post && d.post.visibility };
  };
  const priv = await mk("PRIVATE-DIARY-ENTRY only for me", "private");
  const fr = await mk("FRIENDS-ONLY note about my week", "friends");
  const pub = await mk("PUBLIC-CHECK anyone can read this", "public");
  return JSON.stringify({ priv, fr, pub });
})();
