// SpeakEasy 微信风原型生成器 — Figma 插件
// 运行后会在当前页生成 4 个 artboard（手机/手机语音态/平板/桌面）
// 全部使用 Auto Layout 与原生文本层，可直接改样式

const C = {
  bg: "#EDEDED",
  white: "#FFFFFF",
  text: "#191919",
  text2: "#888888",
  text3: "#B2B2B2",
  line: "#00000017",
  lineSoft: "#0000000D",
  green: "#07C160",
  greenInk: "#06AD56",
  bubbleMe: "#95EC69",
  red: "#FA5151",
  bar: "#F7F7F7",
  blue: "#576B95",
};

const rgb = (hex) => {
  const h = hex.replace("#", "");
  return {
    r: parseInt(h.slice(0, 2), 16) / 255,
    g: parseInt(h.slice(2, 4), 16) / 255,
    b: parseInt(h.slice(4, 6), 16) / 255,
  };
};
const solid = (hex, opacity = 1) => ({
  type: "SOLID",
  color: rgb(hex),
  opacity,
});

let FONT = { family: "Inter", style: "Regular" };
let FONT_B = { family: "Inter", style: "Bold" };

// ---------- 基础节点工具 ----------
function frame(parent, opts) {
  const f = figma.createFrame();
  f.name = opts.name || "frame";
  f.resize(opts.w || 100, opts.h || 100);
  f.fills = opts.fill ? [solid(opts.fill)] : [];
  f.strokes = opts.stroke ? [solid(opts.stroke)] : [];
  if (opts.stroke) f.strokeWeight = 1;
  f.cornerRadius = opts.radius || 0;
  if (opts.clip !== undefined) f.clipsContent = opts.clip;
  if (opts.layout) {
    f.layoutMode = opts.layout;
    f.primaryAxisSizingMode = "FIXED";
    f.counterAxisSizingMode = "FIXED";
    f.paddingLeft = opts.pad?.[0] ?? 0;
    f.paddingRight = opts.pad?.[1] ?? 0;
    f.paddingTop = opts.pad?.[2] ?? 0;
    f.paddingBottom = opts.pad?.[3] ?? 0;
    f.itemSpacing = opts.gap ?? 0;
    f.primaryAxisAlignItems = opts.mainAlign || "MIN";
    f.counterAxisAlignItems = opts.crossAlign || "MIN";
  }
  parent.appendChild(f);
  return f;
}

function txt(parent, s, o = {}) {
  const t = figma.createText();
  t.fontName = o.bold ? FONT_B : FONT;
  t.characters = s;
  t.fontSize = o.size || 14;
  t.fills = [solid(o.color || C.text)];
  if (o.lh) t.lineHeight = { value: o.lh, unit: "PIXELS" };
  if (o.opacity) t.opacity = o.opacity;
  if (o.align) t.textAlignHorizontal = o.align;
  parent.appendChild(t);
  return t;
}

function avatar(parent, letter, from, to, size = 44) {
  const a = figma.createFrame();
  a.resize(size, size);
  a.cornerRadius = size > 40 ? 6 : 5;
  a.layoutMode = "HORIZONTAL";
  a.primaryAxisSizingMode = "FIXED";
  a.counterAxisSizingMode = "FIXED";
  a.primaryAxisAlignItems = "CENTER";
  a.counterAxisAlignItems = "CENTER";
  a.fills = [
    {
      type: "GRADIENT_LINEAR",
      gradientTransform: [
        [1, 0, 0],
        [0, 1, 0],
      ],
      gradientStops: [
        { position: 0, color: { ...rgb(from), a: 1 } },
        { position: 1, color: { ...rgb(to), a: 1 } },
      ],
    },
  ];
  const t = figma.createText();
  t.fontName = FONT_B;
  t.characters = letter;
  t.fontSize = size > 40 ? 15 : 12;
  t.fills = [solid("#FFFFFF")];
  a.appendChild(t);
  parent.appendChild(a);
  return a;
}

function row(parent, dir = "HORIZONTAL", gap = 0) {
  const f = figma.createFrame();
  f.fills = [];
  f.layoutMode = dir;
  f.primaryAxisSizingMode = "AUTO";
  f.counterAxisSizingMode = "AUTO";
  f.primaryAxisAlignItems = "MIN";
  f.counterAxisAlignItems = dir === "HORIZONTAL" ? "MIN" : "MAX";
  f.itemSpacing = gap;
  parent.appendChild(f);
  return f;
}

// ---------- 组件 ----------
function navBar(parent, name, sub) {
  const n = frame(parent, {
    name: "导航栏",
    w: 390,
    h: 52,
    fill: C.bg,
  });
  n.layoutMode = "HORIZONTAL";
  n.primaryAxisSizingMode = "FIXED";
  n.counterAxisSizingMode = "FIXED";
  n.paddingLeft = 8;
  n.paddingRight = 8;
  n.counterAxisAlignItems = "CENTER";

  const back = frame(n, { name: "返回", w: 34, h: 34 });
  txt(back, "‹", { size: 26, color: C.text }).y = -4;

  const mid = frame(n, { name: "标题区", w: 260, h: 40 });
  mid.layoutMode = "VERTICAL";
  mid.primaryAxisSizingMode = "FIXED";
  mid.counterAxisSizingMode = "FIXED";
  mid.counterAxisAlignItems = "CENTER";
  mid.primaryAxisAlignItems = "CENTER";
  const t1 = txt(mid, name, { size: 16.5, bold: true });
  t1.textAlignHorizontal = "CENTER";
  const t2 = txt(mid, sub || " ", { size: 11, color: C.text2 });
  t2.textAlignHorizontal = "CENTER";

  const icons = row(n, "HORIZONTAL", 2);
  txt(icons, "📞", { size: 17 });
  txt(icons, "⋯", { size: 19, color: C.text });
  const spacer = frame(n, { name: "spacer", w: 0, h: 0 });
  n.appendChild(spacer);
  return n;
}

function dayDivider(parent, label) {
  const w = row(parent, "HORIZONTAL", 0);
  w.appendChild(frame(w, { name: "s1", w: 120, h: 1 }));
  const chip = frame(w, {
    name: "日期",
    w: 90,
    h: 20,
    fill: C.white,
    radius: 4,
  });
  chip.layoutMode = "HORIZONTAL";
  chip.primaryAxisAlignItems = "CENTER";
  chip.counterAxisAlignItems = "CENTER";
  txt(chip, label, { size: 11.5, color: C.text3 });
  return w;
}

function voiceBubble(parent, dur, mood, mine = false, width = 190) {
  const b = frame(parent, {
    name: "语音气泡",
    w: width,
    h: mood ? 74 : 52,
    fill: mine ? C.bubbleMe : C.white,
    radius: 10,
  });
  b.layoutMode = "VERTICAL";
  b.primaryAxisSizingMode = "FIXED";
  b.counterAxisSizingMode = "FIXED";
  b.paddingLeft = 11;
  b.paddingRight = 11;
  b.paddingTop = 9;
  b.paddingBottom = 9;
  b.itemSpacing = 6;
  b.primaryAxisAlignItems = "CENTER";
  b.counterAxisAlignItems = mine ? "MAX" : "MIN";

  if (mood) {
    const chip = frame(b, {
      name: "情绪",
      w: 62,
      h: 17,
      fill: "#00000012",
      radius: 4,
    });
    chip.layoutMode = "HORIZONTAL";
    chip.primaryAxisAlignItems = "CENTER";
    chip.counterAxisAlignItems = "CENTER";
    txt(chip, mood, { size: 10.5, color: C.text2 });
  }

  const r = row(b, "HORIZONTAL", 9);
  r.counterAxisAlignItems = "CENTER";
  const wave = row(r, "HORIZONTAL", 1.5);
  wave.counterAxisAlignItems = "CENTER";
  const hs = [8, 14, 19, 10, 16, 7, 13, 18, 9, 14, 6, 11];
  hs.forEach((h, i) => {
    const bar = frame(wave, { name: "bar", w: 2, h: h, fill: "#0000006E", radius: 1 });
    if (i < 3) bar.fills = [solid(mine ? "#0000008C" : C.greenInk)];
  });
  txt(r, dur, { size: 12.5, color: "#0000008C" });
  return b;
}

function textBubble(parent, s, mine = false, width = 236) {
  const b = frame(parent, {
    name: "文字气泡",
    w: width,
    h: 56,
    fill: mine ? C.bubbleMe : C.white,
    radius: 10,
  });
  b.layoutMode = "VERTICAL";
  b.primaryAxisSizingMode = "FIXED";
  b.counterAxisSizingMode = "FIXED";
  b.paddingLeft = 11;
  b.paddingRight = 11;
  b.paddingTop = 9;
  b.paddingBottom = 9;
  b.counterAxisAlignItems = mine ? "MAX" : "MIN";
  const t = txt(b, s, { size: 15.5, lh: 21 });
  t.textAlignHorizontal = mine ? "RIGHT" : "LEFT";
  return b;
}

function transCard(parent, body) {
  const c = frame(parent, {
    name: "翻译卡",
    w: 250,
    h: 92,
    fill: C.white,
    radius: 8,
  });
  c.layoutMode = "VERTICAL";
  c.primaryAxisSizingMode = "FIXED";
  c.counterAxisSizingMode = "FIXED";
  c.paddingLeft = 10;
  c.paddingRight = 10;
  c.paddingTop = 8;
  c.paddingBottom = 8;
  c.itemSpacing = 4;
  const h = row(c, "HORIZONTAL", 5);
  h.counterAxisAlignItems = "CENTER";
  txt(h, "译 · 简体中文", { size: 11, color: C.text3 });
  txt(h, "  收起", { size: 11, color: C.blue });
  txt(c, body, { size: 12.5, color: "#4A4A4A", lh: 18 });
  return c;
}

function suggestPill(parent, text) {
  const p = frame(parent, {
    name: "更地道",
    w: 232,
    h: 26,
    fill: C.white,
    radius: 13,
  });
  p.layoutMode = "HORIZONTAL";
  p.primaryAxisSizingMode = "FIXED";
  p.counterAxisSizingMode = "FIXED";
  p.paddingLeft = 9;
  p.paddingRight = 10;
  p.counterAxisAlignItems = "CENTER";
  p.itemSpacing = 5;
  txt(p, "✨", { size: 11 });
  txt(p, text, { size: 12.5, color: C.greenInk });
  return p;
}

function imageBubble(parent, w = 150, h = 112) {
  const i = frame(parent, {
    name: "图片",
    w: w,
    h: h,
    radius: 8,
  });
  i.fills = [
    {
      type: "GRADIENT_LINEAR",
      gradientTransform: [
        [1, 0.4, 0],
        [0, 1, 0],
      ],
      gradientStops: [
        { position: 0, color: { ...rgb("#C9D6FF"), a: 1 } },
        { position: 0.5, color: { ...rgb("#E2E2E2"), a: 1 } },
        { position: 1, color: { ...rgb("#FFD6C9"), a: 1 } },
      ],
    },
  ];
  i.layoutMode = "HORIZONTAL";
  i.primaryAxisAlignItems = "CENTER";
  i.counterAxisAlignItems = "CENTER";
  txt(i, "📷 你发的咖啡", { size: 11.5, color: "#00000059" });
  return i;
}

function typingBubble(parent) {
  const b = frame(parent, { name: "正在输入", w: 58, h: 34, fill: C.white, radius: 10 });
  b.layoutMode = "HORIZONTAL";
  b.primaryAxisAlignItems = "CENTER";
  b.counterAxisAlignItems = "CENTER";
  b.itemSpacing = 3;
  ["#B2B2B2", "#DADADA", "#B2B2B2"].forEach((col) => {
    frame(b, { name: "dot", w: 5, h: 5, fill: col, radius: 3 });
  });
  return b;
}

function composer(parent, voiceMode = false) {
  const c = frame(parent, {
    name: "输入区",
    w: 390,
    h: voiceMode ? 78 : 60,
    fill: C.bar,
  });
  c.layoutMode = "HORIZONTAL";
  c.primaryAxisSizingMode = "FIXED";
  c.counterAxisSizingMode = "FIXED";
  c.paddingLeft = 8;
  c.paddingRight = 8;
  c.paddingTop = 7;
  c.paddingBottom = 7;
  c.counterAxisAlignItems = "CENTER";
  c.itemSpacing = 7;

  txt(c, voiceMode ? "⌨️" : "🎙", { size: 19 });

  if (voiceMode) {
    const hold = frame(c, { name: "按住说话", w: 300, h: 36, fill: C.white, radius: 6 });
    hold.layoutMode = "HORIZONTAL";
    hold.primaryAxisSizingMode = "FIXED";
    hold.counterAxisSizingMode = "FIXED";
    hold.primaryAxisAlignItems = "CENTER";
    hold.counterAxisAlignItems = "CENTER";
    txt(hold, "按住 说话", { size: 15, bold: true });
    txt(c, "＋", { size: 19 });
  } else {
    const inp = frame(c, { name: "输入框", w: 268, h: 36, fill: C.white, radius: 6 });
    inp.layoutMode = "HORIZONTAL";
    inp.primaryAxisSizingMode = "FIXED";
    inp.counterAxisSizingMode = "FIXED";
    inp.paddingLeft = 10;
    inp.counterAxisAlignItems = "CENTER";
    txt(inp, "发消息…", { size: 15, color: C.text3 });
    txt(c, "☺", { size: 19 });
    txt(c, "⊕", { size: 19 });
  }
  return c;
}

function tabBar(parent) {
  const t = frame(parent, { name: "Tab", w: 390, h: 56, fill: C.bar });
  t.layoutMode = "HORIZONTAL";
  t.primaryAxisSizingMode = "FIXED";
  t.counterAxisSizingMode = "FIXED";
  t.primaryAxisAlignItems = "SPACE_EVENLY";
  t.counterAxisAlignItems = "CENTER";

  const mk = (icon, label, on) => {
    const c = frame(t, { name: "tab-" + label, w: 100, h: 44 });
    c.layoutMode = "VERTICAL";
    c.primaryAxisSizingMode = "FIXED";
    c.counterAxisSizingMode = "FIXED";
    c.primaryAxisAlignItems = "CENTER";
    c.counterAxisAlignItems = "CENTER";
    c.itemSpacing = 2;
    const ic = txt(c, icon, { size: 20 });
    ic.textAlignHorizontal = "CENTER";
    const lb = txt(c, label, { size: 10.5, color: on ? C.green : C.text2 });
    lb.textAlignHorizontal = "CENTER";
    return c;
  };
  mk("💬", "消息", true);
  mk("🌐", "动态 · 偶遇", false);
  mk("📖", "表达本 · 画像", false);
  return t;
}

function chatRow(parent, side, build) {
  const r = row(parent, "HORIZONTAL", 9);
  r.counterAxisAlignItems = "MIN";
  if (side === "me") {
    // 用反向排列：气泡在左、头像在右
    const holder = frame(r, { name: "me-holder", w: 300, h: 10 });
    holder.layoutMode = "VERTICAL";
    holder.primaryAxisSizingMode = "FIXED";
    holder.counterAxisSizingMode = "AUTO";
    holder.counterAxisAlignItems = "MAX";
    holder.primaryAxisSizingMode = "AUTO";
    build(holder);
    avatar(r, "我", "#95EC69", "#48A53A");
  } else {
    avatar(r, "C", "#8EC5FC", "#4A7CC7");
    const holder = frame(r, { name: "other-holder", w: 300, h: 10 });
    holder.layoutMode = "VERTICAL";
    holder.primaryAxisSizingMode = "AUTO";
    holder.counterAxisSizingMode = "FIXED";
    holder.itemSpacing = 6;
    build(holder);
  }
  return r;
}

function chatBody(parent, w, h) {
  const chat = frame(parent, {
    name: "消息区",
    w: w,
    h: h,
    fill: C.bg,
  });
  chat.layoutMode = "VERTICAL";
  chat.primaryAxisSizingMode = "FIXED";
  chat.counterAxisSizingMode = "FIXED";
  chat.paddingLeft = 12;
  chat.paddingRight = 12;
  chat.paddingTop = 10;
  chat.paddingBottom = 10;
  chat.itemSpacing = 8;
  chat.clipsContent = true;

  dayDivider(chat, "今天 16:08");

  chatRow(chat, "other", (h) => {
    voiceBubble(h, "0:07", "😄 兴奋");
    transCard(h, "兄弟，别提了 —— 我今天算是把那个破中轴给收拾了，咖啡机也终于不闹脾气了。");
  });

  chatRow(chat, "me", (h) => {
    textBubble(h, "Honestly, I very like this coffee. Yesterday I go to the shop.", true);
    suggestPill(h, "更地道：I'm really into this coffee");
  });

  chatRow(chat, "other", (h) => {
    voiceBubble(h, "0:03", "😆 笑", false, 160);
    suggestPill(h, "更地道：that's a rip-off");
  });

  chatRow(chat, "other", (h) => {
    imageBubble(h);
  });

  chatRow(chat, "me", (h) => {
    voiceBubble(h, "0:05", null, true, 148);
  });

  chatRow(chat, "other", (h) => {
    typingBubble(h);
  });

  return chat;
}

// ---------- Artboards ----------
function makePhone(voiceMode) {
  const root = frame(figma.currentPage, {
    name: voiceMode ? "手机 390 · 语音态" : "手机 390 · 消息",
    w: 390,
    h: 844,
    fill: C.bg,
  });
  root.layoutMode = "VERTICAL";
  root.primaryAxisSizingMode = "FIXED";
  root.counterAxisSizingMode = "FIXED";
  root.clipsContent = true;

  navBar(root, "Callum", "对方正在说话…");
  chatBody(root, 390, voiceMode ? 610 : 620);
  composer(root, voiceMode);
  tabBar(root);
  return root;
}

function makeTablet() {
  const root = frame(figma.currentPage, {
    name: "平板 834 · 双栏",
    w: 834,
    h: 1112,
    fill: C.white,
  });
  root.layoutMode = "HORIZONTAL";
  root.primaryAxisSizingMode = "FIXED";
  root.counterAxisSizingMode = "FIXED";
  root.clipsContent = true;

  // 左栏
  const left = frame(root, { name: "会话列表", w: 268, h: 1112, fill: C.white });
  left.layoutMode = "VERTICAL";
  left.primaryAxisSizingMode = "FIXED";
  left.counterAxisSizingMode = "FIXED";
  left.clipsContent = true;

  const ln = frame(left, { name: "列表导航", w: 268, h: 52, fill: C.bg });
  ln.layoutMode = "HORIZONTAL";
  ln.primaryAxisSizingMode = "FIXED";
  ln.counterAxisSizingMode = "FIXED";
  ln.paddingLeft = 14;
  ln.counterAxisAlignItems = "CENTER";
  txt(ln, "消息", { size: 17, bold: true });
  txt(ln, "     🔍  ＋", { size: 15, color: C.text2 });

  const convs = [
    ["C", "Callum", "🇬🇧", "16:13", "[语音] 那你后来怎么跟他说的？", "#8EC5FC", "#4A7CC7"],
    ["M", "Matteo", "🇮🇹", "15:40", "Dai, raccontami com'è andata", "#FFD39B", "#E08B48"],
    ["F", "Freya", "🇬🇧", "昨天", "[语音] Okay, so the flat white was…", "#A7E9AF", "#3F9E67"],
    ["C", "Chloe", "🇺🇸", "周二", "That's mad. Tell me more later?", "#C3B0F7", "#7D5FD1"],
    ["M", "Marcus", "🇦🇺", "周一", "[图片] 周末的咖啡", "#FFC2D4", "#D96A90"],
  ];
  convs.forEach(([l, name, flag, time, msg, f1, f2], idx) => {
    const c = frame(left, {
      name: "会话-" + name,
      w: 268,
      h: 68,
      fill: idx === 0 ? "#E9E9E9" : C.white,
    });
    c.layoutMode = "HORIZONTAL";
    c.primaryAxisSizingMode = "FIXED";
    c.counterAxisSizingMode = "FIXED";
    c.paddingLeft = 14;
    c.paddingRight = 14;
    c.paddingTop = 11;
    c.itemSpacing = 10;
    avatar(c, l, f1, f2, 44);
    const body = frame(c, { name: "内容", w: 180, h: 46 });
    body.layoutMode = "VERTICAL";
    body.primaryAxisSizingMode = "FIXED";
    body.counterAxisSizingMode = "FIXED";
    body.itemSpacing = 3;
    const top = row(body, "HORIZONTAL", 6);
    txt(top, name, { size: 15.5 });
    txt(top, flag, { size: 11, color: C.text3 });
    txt(top, "   " + time, { size: 11.5, color: C.text3 });
    txt(body, msg, { size: 13, color: C.text2 });
  });

  // 右栏
  const right = frame(root, { name: "聊天", w: 566, h: 1112, fill: C.bg });
  right.layoutMode = "VERTICAL";
  right.primaryAxisSizingMode = "FIXED";
  right.counterAxisSizingMode = "FIXED";
  right.clipsContent = true;

  const nb = navBar(right, "Callum", "对方正在说话…");
  nb.resize(566, 52);
  const bodyR = chatBody(right, 566, 940);
  bodyR.resize(566, 940);
  const cp = composer(right, false);
  cp.resize(566, 60);

  return root;
}

function makeDesktop() {
  const root = frame(figma.currentPage, {
    name: "桌面 1440 · 双栏居中",
    w: 1440,
    h: 900,
    fill: "#DCDCDC",
  });
  root.layoutMode = "HORIZONTAL";
  root.primaryAxisSizingMode = "FIXED";
  root.counterAxisSizingMode = "FIXED";
  root.primaryAxisAlignItems = "CENTER";
  root.counterAxisAlignItems = "CENTER";

  const win = frame(root, {
    name: "窗口 1180×860",
    w: 1180,
    h: 860,
    fill: C.white,
    radius: 12,
  });
  win.layoutMode = "HORIZONTAL";
  win.primaryAxisSizingMode = "FIXED";
  win.counterAxisSizingMode = "FIXED";
  win.clipsContent = true;

  const left = frame(win, { name: "会话列表", w: 320, h: 860, fill: C.white });
  left.layoutMode = "VERTICAL";
  left.primaryAxisSizingMode = "FIXED";
  left.counterAxisSizingMode = "FIXED";
  left.clipsContent = true;

  const ln = frame(left, { name: "列表导航", w: 320, h: 52, fill: C.bg });
  ln.layoutMode = "HORIZONTAL";
  ln.primaryAxisSizingMode = "FIXED";
  ln.counterAxisSizingMode = "FIXED";
  ln.paddingLeft = 14;
  ln.counterAxisAlignItems = "CENTER";
  txt(ln, "消息", { size: 17, bold: true });
  txt(ln, "          🔍   ＋", { size: 15, color: C.text2 });

  const names = [
    ["C", "Callum", "🇬🇧", "16:13", "[语音] 那你后来怎么跟他说的？", "#8EC5FC", "#4A7CC7"],
    ["M", "Matteo", "🇮🇹", "15:40", "Dai, raccontami com'è andata", "#FFD39B", "#E08B48"],
    ["F", "Freya", "🇬🇧", "昨天", "[语音] Okay, so the flat white was…", "#A7E9AF", "#3F9E67"],
    ["C", "Chloe", "🇺🇸", "周二", "That's mad. Tell me more later?", "#C3B0F7", "#7D5FD1"],
  ];
  names.forEach(([l, name, flag, time, msg, f1, f2], idx) => {
    const c = frame(left, {
      name: "会话-" + name,
      w: 320,
      h: 68,
      fill: idx === 0 ? "#E9E9E9" : C.white,
    });
    c.layoutMode = "HORIZONTAL";
    c.primaryAxisSizingMode = "FIXED";
    c.counterAxisSizingMode = "FIXED";
    c.paddingLeft = 14;
    c.paddingRight = 14;
    c.paddingTop = 11;
    c.itemSpacing = 10;
    avatar(c, l, f1, f2, 44);
    const body = frame(c, { name: "内容", w: 232, h: 46 });
    body.layoutMode = "VERTICAL";
    body.primaryAxisSizingMode = "FIXED";
    body.counterAxisSizingMode = "FIXED";
    body.itemSpacing = 3;
    const top = row(body, "HORIZONTAL", 6);
    txt(top, name, { size: 15.5 });
    txt(top, flag, { size: 11, color: C.text3 });
    txt(top, "      " + time, { size: 11.5, color: C.text3 });
    txt(body, msg, { size: 13, color: C.text2 });
  });

  const right = frame(win, { name: "聊天", w: 860, h: 860, fill: C.bg });
  right.layoutMode = "VERTICAL";
  right.primaryAxisSizingMode = "FIXED";
  right.counterAxisSizingMode = "FIXED";
  right.clipsContent = true;

  const nb = navBar(right, "Callum", "对方正在说话…");
  nb.resize(860, 52);
  const bodyR = chatBody(right, 860, 748);
  bodyR.resize(860, 748);
  const cp = composer(right, false);
  cp.resize(860, 60);

  return root;
}

// ---------- 入口 ----------
(async () => {
  // 字体：优先 Inter，失败退回默认
  try {
    await figma.loadFontAsync({ family: "Inter", style: "Regular" });
    await figma.loadFontAsync({ family: "Inter", style: "Bold" });
  } catch (e) {
    FONT = figma.listAvailableFontsAsync
      ? (await figma.listAvailableFontsAsync())[0].fontName
      : { family: "Roboto", style: "Regular" };
    FONT_B = FONT;
    await figma.loadFontAsync(FONT);
  }

  const made = [
    makePhone(false),
    makePhone(true),
    makeTablet(),
    makeDesktop(),
  ];

  // 排成一排，方便看
  let x = 0;
  made.forEach((f) => {
    f.x = x;
    f.y = 0;
    x += f.width + 80;
  });

  figma.viewport.scrollAndZoomIntoView(made);
  figma.notify(`已生成 ${made.length} 个原型画板`);
  figma.closePlugin();
})().catch((e) => {
  figma.notify("生成失败: " + e.message, { error: true });
  figma.closePlugin();
});
