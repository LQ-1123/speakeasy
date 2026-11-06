(async () => {
  const r = await fetch("/api/v3/events");
  const data = await r.json();
  return JSON.stringify({
    unread: data.unread,
    items: (data.items || []).map((e) => ({
      type: e.type,
      status: e.status,
      persona: e.persona && e.persona.nickname,
      text: e.payload && (e.payload.comment || e.payload.message || e.payload.reaction),
    })),
  });
})();
