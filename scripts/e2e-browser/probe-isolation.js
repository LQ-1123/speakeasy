(async () => {
  const me = JSON.parse(localStorage.getItem("speakeasy.identity.v1") || "{}");
  const friends = await (await fetch("/api/v2/friends")).json();
  const feed = await (await fetch("/api/v3/feed?limit=40")).json();
  const events = await (await fetch("/api/v3/events")).json();
  const posts = (feed.items || []).filter((x) => x.kind === "user_post");
  return JSON.stringify({
    user_id: me.user_id,
    friends: (friends.friends || []).length,
    user_posts_visible: posts.length,
    post_visibilities: Array.from(new Set(posts.map((p) => p.visibility))),
    events: (events.items || []).length,
    personas_visible: (feed.items || []).filter((x) => x.kind === "persona_post").length,
  });
})();
