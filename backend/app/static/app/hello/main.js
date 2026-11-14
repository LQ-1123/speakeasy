(() => {
  "use strict";
  const downloadUrl = "https://47-104-24-236.sslip.io/api/app/download/speakeasy-4.3.2-432.apk";
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  // Progressively enhance: all core content and download links work without JS.
  if ("IntersectionObserver" in window && !reducedMotion.matches) {
    document.documentElement.classList.add("js-reveal");
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          observer.unobserve(entry.target);
        }
      }
    }, { threshold: 0.08 });
    document.querySelectorAll(".reveal").forEach((element) => observer.observe(element));
  }

  const tabs = Array.from(document.querySelectorAll("[data-tab]"));
  function selectTab(tab, moveFocus = false) {
    for (const item of tabs) {
      const selected = item === tab;
      item.classList.toggle("is-active", selected);
      item.setAttribute("aria-selected", String(selected));
      item.tabIndex = selected ? 0 : -1;
      document.getElementById(item.getAttribute("aria-controls")).hidden = !selected;
    }
    if (moveFocus) tab.focus();
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => selectTab(tab));
    tab.addEventListener("keydown", (event) => {
      let next;
      if (event.key === "ArrowDown") next = (index + 1) % tabs.length;
      else if (event.key === "ArrowUp") next = (index - 1 + tabs.length) % tabs.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = tabs.length - 1;
      else return;
      event.preventDefault();
      selectTab(tabs[next], true);
    });
  });

  const translateButton = document.querySelector(".translate-button");
  translateButton.addEventListener("click", () => {
    const show = translateButton.getAttribute("aria-pressed") !== "true";
    translateButton.setAttribute("aria-pressed", String(show));
    translateButton.querySelector("span").textContent = show ? "收起中文翻译" : "看看中文意思";
    document.querySelectorAll(".translation").forEach((item) => { item.hidden = !show; item.lang = "zh-CN"; });
  });

  const toast = document.querySelector(".toast");
  let toastTimer;
  function notify(message) {
    clearTimeout(toastTimer);
    toast.textContent = message;
    toast.hidden = false;
    toastTimer = setTimeout(() => { toast.hidden = true; }, 4500);
  }
  document.querySelector(".copy-link").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(downloadUrl);
      notify("下载链接已复制，可以发送到你的安卓手机。");
    } catch {
      const dialog = document.querySelector(".copy-dialog");
      dialog.showModal();
      const input = dialog.querySelector("input");
      input.focus();
      input.select();
    }
  });
})();
