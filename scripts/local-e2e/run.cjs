// 本地模式端到端测试：用真实 key 跑 资料 → 捏人 → 通过 → 聊天 → 语音合成
// 运行前先 tsc 编译（见 run.ps1）

const fs = require("fs");
const path = require("path");

/* ---------------- 浏览器环境垫片 ---------------- */
globalThis.window = globalThis;
const mem = new Map();

globalThis.localStorage = {
  getItem: (k) => (mem.has(`ls:${k}`) ? mem.get(`ls:${k}`) : null),
  setItem: (k, v) => mem.set(`ls:${k}`, String(v)),
  removeItem: (k) => mem.delete(`ls:${k}`),
};

function makeStore() {
  return {
    get(key) {
      const req = {};
      queueMicrotask(() => {
        req.result = mem.get(`idb:${key}`);
        if (req.onsuccess) req.onsuccess();
      });
      return req;
    },
    put(value, key) {
      mem.set(`idb:${key}`, value);
      return {};
    },
  };
}

const fakeDb = {
  objectStoreNames: { contains: () => true },
  createObjectStore: () => {},
  transaction: () => {
    const tx = {
      objectStore: () => makeStore(),
      oncomplete: null,
      onerror: null,
      onabort: null,
    };
    queueMicrotask(() => tx.oncomplete && tx.oncomplete());
    return tx;
  },
};

globalThis.indexedDB = {
  open: () => {
    const req = {};
    queueMicrotask(() => {
      req.result = fakeDb;
      if (req.onupgradeneeded) req.onupgradeneeded();
      if (req.onsuccess) req.onsuccess();
    });
    return req;
  },
};

if (typeof URL.createObjectURL !== "function") {
  URL.createObjectURL = () => "blob:mock-e2e";
}

/* ---------------- 读取真实 key（不打印） ---------------- */
const root = path.resolve(__dirname, "..", "..");
function readJson(p) {
  return JSON.parse(fs.readFileSync(path.join(root, p), "utf8"));
}
const llm = readJson("data/llm_config.json");
const stt = readJson("data/stt_config.json");
const tts = readJson("data/tts_config.json");

const keys = {
  llmBase: llm.base_url || "https://api.deepseek.com/v1",
  llmKey: llm.api_key || "",
  llmModel: llm.model || "deepseek-flash",
  sttBase: stt.base_url || "https://api.siliconflow.cn/v1",
  sttKey: stt.api_key || "",
  sttModel: stt.model || "FunAudioLLM/SenseVoiceSmall",
  fishBase: tts.fish_base_url || "https://api.fish.audio",
  fishKey: tts.fish_api_key || "",
  fishModel: tts.fish_model || "s2.1-pro-free",
  fishRef: tts.fish_reference_id || "",
};
localStorage.setItem("speakeasy.local.keys.v1", JSON.stringify(keys));

/* ---------------- 跑真实流程 ---------------- */
const engine = require("./build/local/engine.js");

(async () => {
  const t0 = Date.now();
  console.log("1) 保存资料…");
  const ok = await engine.localSaveProfile("测试用户", ["en"], "");
  console.log("   保存结果:", ok);

  console.log("2) 等待捏人（LLM + TTS，最多 90s）…");
  let requests = [];
  for (let i = 0; i < 45; i += 1) {
    await new Promise((r) => setTimeout(r, 2000));
    const list = await engine.localLoadFriendList();
    requests = list.requests;
    if (requests.length >= 1) break;
  }
  console.log("   申请数量:", requests.length);
  if (!requests.length) {
    console.log("FAIL: 没有生成好友申请");
    process.exit(1);
  }
  const req = requests[0];
  console.log("   申请:", req.name, "| 年龄:", req.age, "| 城市:", req.city, "| 有招呼语音:", Boolean(req.greetingAudio));

  console.log("3) 通过申请…");
  await engine.localAcceptFriend(req.id);

  console.log("4) 发一条文字消息（真实 DeepSeek + 本地 TTS）…");
  const msgs = await engine.localSendText(req.id, "Hi! What are you up to today?");
  const mine = (msgs || []).filter((m) => m.from === "me");
  const theirs = (msgs || []).filter((m) => m.from === "friend");
  console.log("   我的消息:", mine.length, "| 对方回复:", theirs.length);
  const reply = theirs[theirs.length - 1];
  console.log("   回复文本:", (reply && reply.text || "").slice(0, 120));
  console.log("   回复音频:", reply && reply.audio ? "有" : "无", "| 时长ms:", reply && reply.durationMs);

  console.log("5) 翻译…");
  const zh = await engine.localTranslate(reply.text);
  console.log("   中文:", (zh || "").slice(0, 80));

  const pass = requests.length > 0 && theirs.length > 0 && Boolean(reply.text) && Boolean(reply.audio);
  console.log(`\n结果: ${pass ? "PASS" : "FAIL"}（耗时 ${((Date.now() - t0) / 1000).toFixed(1)}s）`);
  process.exit(pass ? 0 : 2);
})().catch((e) => {
  console.error("EXC:", e && e.stack ? e.stack.slice(0, 800) : e);
  process.exit(3);
});
