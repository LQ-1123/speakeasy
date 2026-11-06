(() => {
  const btns = Array.from(document.querySelectorAll(".w4-btn")).map((b) => b.innerText.trim());
  const toast = document.querySelector(".w4-toast");
  const hash = location.hash;
  const text = document.body.innerText;
  return JSON.stringify({
    hash,
    btns,
    toast: toast ? toast.innerText : null,
    hasQuLiaoTian: text.includes("去聊天"),
    hasAddFriend: text.includes("添加好友"),
    identity: localStorage.getItem("speakeasy.identity.v1"),
  });
})();
