JSON.stringify({
  title: document.title,
  hash: location.hash,
  bodyLen: document.body.innerText.length,
  head: document.body.innerText.slice(0, 500),
  cards: document.querySelectorAll(".w4-moment").length,
  heads: Array.from(document.querySelectorAll(".w4-head-title")).map((e) => e.innerText),
  ls: Object.keys(localStorage).reduce((acc, k) => {
    acc[k] = String(localStorage.getItem(k)).slice(0, 60);
    return acc;
  }, {}),
});
