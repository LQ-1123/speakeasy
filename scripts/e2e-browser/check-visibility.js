(async () => {
  const feed = await (await fetch("/api/v3/feed?limit=40")).json();
  const items = feed.items || [];
  const texts = items.map((x) => x.content);
  return JSON.stringify({
    total: items.length,
    sees_private: texts.some((t) => t.includes("PRIVATE-DIARY-ENTRY")),
    sees_friends: texts.some((t) => t.includes("FRIENDS-ONLY")),
    sees_public: texts.some((t) => t.includes("PUBLIC-CHECK")),
  });
})();
