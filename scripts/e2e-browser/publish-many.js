(async () => {
  const posts = [
    { content: "Early morning 10k run by the river, then a flat white at my favourite cafe", tags: ["running", "coffee"] },
    { content: "Reading a long novel on the balcony while my cat supervises", tags: ["books", "cats", "tea"] },
    { content: "Bouldering session at the gym, finally sent the blue route", tags: ["climbing", "fitness"] },
    { content: "Built a new synth patch that sounds like rain on a window", tags: ["music", "engineering"] },
    { content: "Booked a flight to Lisbon for next month, can't wait to see the architecture", tags: ["travel", "architecture"] },
  ];
  const statuses = [];
  for (const p of posts) {
    const r = await fetch("/api/v3/posts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...p, visibility: "public" }),
    });
    statuses.push(r.status);
  }
  return JSON.stringify({ statuses });
})();
