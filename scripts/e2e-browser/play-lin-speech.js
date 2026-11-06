(() => {
  const articles = Array.from(document.querySelectorAll(".w4-moment"));
  const target = articles.find((a) => a.innerText.includes("熟食中心"));
  if (!target) return JSON.stringify({ found: false, count: articles.length });
  const btn = Array.from(target.querySelectorAll("button")).find((b) => b.innerText.trim() === "朗读");
  if (!btn) return JSON.stringify({ found: true, button: false });
  btn.click();
  return JSON.stringify({ found: true, button: true, clicked: btn.innerText.trim() });
})();
