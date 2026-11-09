"""管理端页面：**表单化**的词库编辑器（服务端直出 HTML，不进客户端 bundle）。

设计要点
--------
* 不要求用户写 JSON：每个 key → value 都是一个可视化"标签"（chip）；
* DNA：每个字段一张卡片，取值就是标签，可点开关（启用/停用）、拖动数值滑杆（0..1）、
  点星号设为默认；"新增取值"直接在卡片里填 key + 中文名 + 数值；
* 场景 / 口头禅 / 禁用词 / 素材池：都是标签云，输入框回车即可加、点 × 即可删；
* 保存前调用后端校验，校验不过就把问题列出来**且不会落盘**；
* 底部保留一个折叠的"JSON 只读预览"，方便排查，但**不是主要编辑方式**。
"""

import json

ADMIN_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SpeakEasy · 词库管理</title>
<style>
  :root { color-scheme: light dark; --bg:#0e1116; --panel:#161a22; --panel2:#1d2230;
          --line:#2a3040; --text:#e9ecf1; --dim:#93a0b4; --accent:#5bd1a5;
          --warn:#ffb454; --bad:#ff6f6f; --chip:#222a38; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:14px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }
  header { position:sticky; top:0; z-index:10; background:rgba(14,17,22,.94);
           backdrop-filter:blur(6px); border-bottom:1px solid var(--line);
           padding:12px 20px; display:flex; align-items:center; gap:12px; }
  header h1 { font-size:15px; margin:0; font-weight:650; letter-spacing:.2px; }
  header .sub { color:var(--dim); font-size:12px; }
  header .grow { flex:1; }
  .pill { font-size:11.5px; padding:2px 9px; border-radius:99px; border:1px solid var(--line);
          color:var(--dim); white-space:nowrap; }
  main { display:grid; grid-template-columns:220px minmax(0,1fr); min-height:calc(100vh - 53px); }
  aside { border-right:1px solid var(--line); padding:12px; }
  .navitem { padding:9px 11px; border-radius:8px; cursor:pointer; margin-bottom:4px;
             border:1px solid transparent; }
  .navitem:hover { background:var(--panel); }
  .navitem.active { background:var(--panel2); border-color:var(--accent); }
  .navitem .n { font-weight:600; font-size:13px; display:flex; gap:7px; align-items:center; }
  .navitem .s { color:var(--dim); font-size:11.5px; margin-top:2px; padding-left:15px; }
  .dot { width:8px; height:8px; border-radius:50%; display:inline-block; flex:none; }
  .dot.ok { background:var(--accent); } .dot.bad { background:var(--bad); }
  .dot.miss { background:var(--dim); }
  section { padding:18px 22px 60px; overflow:auto; }
  .bar { display:flex; align-items:flex-start; gap:12px; margin-bottom:14px; }
  .bar h2 { font-size:16px; margin:0; }
  .bar p { margin:3px 0 0; color:var(--dim); font-size:12.5px; max-width:70ch; }
  .bar .grow { flex:1; }
  button { background:var(--panel2); color:var(--text); border:1px solid var(--line);
           border-radius:8px; padding:7px 14px; cursor:pointer; font-size:13px; }
  button:hover { border-color:var(--accent); }
  button.primary { border-color:var(--accent); color:var(--accent); font-weight:600; }
  button.ghost { background:transparent; }
  button.mini { padding:3px 9px; font-size:12px; border-radius:6px; }
  button.danger:hover { border-color:var(--bad); color:var(--bad); }
  button:disabled { opacity:.45; cursor:not-allowed; }
  .msg { display:none; margin:0 0 14px; padding:10px 14px; border-radius:8px; font-size:13px;
         white-space:pre-wrap; }
  .msg.show { display:block; }
  .msg.ok { background:rgba(91,209,165,.1); border:1px solid var(--accent); }
  .msg.bad { background:rgba(255,111,111,.1); border:1px solid var(--bad); }
  .msg.warn { background:rgba(255,180,84,.1); border:1px solid var(--warn); }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); gap:14px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:13px 14px; }
  .card .head { display:flex; align-items:baseline; gap:8px; margin-bottom:10px; }
  .card .head .zh { font-weight:650; font-size:13.5px; }
  .card .head .code { color:var(--dim); font-size:11.5px; font-family:ui-monospace,Consolas,monospace; }
  .card .head .grow { flex:1; }
  .defaultpick { color:var(--dim); font-size:11.5px; }
  .chips { display:flex; flex-wrap:wrap; gap:8px; }
  /* 简单标签（场景 / 口头禅 / 素材）：一行一个，不换行 */
  .chip { display:inline-flex; align-items:center; gap:7px; background:var(--chip);
          border:1px solid var(--line); border-radius:999px; padding:3px 6px 3px 11px;
          font-size:12.5px; max-width:100%; white-space:nowrap; }
  .chip.off { opacity:.45; border-style:dashed; }
  .chip .vzh { font-weight:600; white-space:nowrap; }
  .chip .vkey { color:var(--dim); font-size:11px; font-family:ui-monospace,Consolas,monospace; white-space:nowrap; }
  .chip .x { cursor:pointer; color:var(--dim); background:transparent; border:0;
             font-size:15px; line-height:1; padding:0 2px; }
  .chip .x:hover { color:var(--bad); }
  .chip .use { color:var(--dim); font-size:11px; white-space:nowrap; }
  /* DNA 取值：两行小卡片（第一行名字/用量/开关，第二行数值滑杆/默认） */
  .vals { display:grid; grid-template-columns:repeat(auto-fill,minmax(196px,1fr)); gap:8px; }
  .val { background:var(--chip); border:1px solid var(--line); border-radius:10px; padding:7px 9px 6px; }
  .val.off { opacity:.5; border-style:dashed; }
  .val .vtop { display:flex; align-items:baseline; gap:6px; }
  .val .vtop .vzh { font-weight:650; font-size:12.5px; white-space:nowrap; }
  .val .vtop .vkey { color:var(--dim); font-size:10.5px; font-family:ui-monospace,Consolas,monospace; overflow:hidden; text-overflow:ellipsis; }
  .val .vtop .grow { flex:1; }
  .val .vtop .use { color:var(--dim); font-size:10.5px; white-space:nowrap; }
  .val .vbottom { display:flex; align-items:center; gap:7px; margin-top:5px; }
  .val .vbottom input[type=range] { flex:1; min-width:60px; accent-color:var(--accent); }
  .val .tnum { width:32px; color:var(--accent); font:11.5px ui-monospace,Consolas,monospace; text-align:right; }
  .val .star { cursor:pointer; color:var(--dim); font-size:14px; line-height:1;
               background:transparent; border:0; padding:0; }
  .val .star.on { color:var(--warn); }
  .val .toggle { cursor:pointer; color:var(--dim); font-size:10.5px; border:1px solid var(--line);
                 border-radius:99px; padding:0 6px; background:transparent; white-space:nowrap; }
  .val .toggle.on { color:var(--accent); border-color:var(--accent); }
  .addrow { display:flex; gap:6px; margin-top:10px; flex-wrap:wrap; }
  .card.off { opacity:.55; }
  .kvrow { display:flex; align-items:center; gap:10px; margin-top:9px; flex-wrap:wrap; }
  .kvleft { min-width:132px; display:flex; flex-direction:column; }
  .kvrow .klabel { color:var(--dim); font-size:12px; }
  .kvrow input[type=range] { flex:1; min-width:90px; accent-color:var(--accent); }
  .knum { width:132px; background:var(--bg); color:var(--accent); border:1px solid var(--line);
          border-radius:6px; padding:2px 7px; font:12px ui-monospace,Consolas,monospace; }
  .kvrow input[type=checkbox] { accent-color:var(--accent); }
  .kvrow input.text { flex:1; min-width:150px; background:var(--bg); color:var(--text);
          border:1px solid var(--line); border-radius:7px; padding:5px 9px; font-size:12.5px; }
  .cfginput { flex:1; min-width:180px; background:var(--bg); color:var(--text);
             border:1px solid var(--line); border-radius:7px; padding:6px 10px; font-size:12.5px; }
  .langsel { background:var(--panel2); color:var(--text); border:1px solid var(--line);
             border-radius:8px; padding:6px 10px; font-size:12.5px; }
  .seg { display:inline-flex; border:1px solid var(--line); border-radius:8px; overflow:hidden; }
  .segbtn { border:0; border-radius:0; background:transparent; padding:4px 10px; font-size:12px; color:var(--dim); }
  .segbtn + .segbtn { border-left:1px solid var(--line); }
  .segbtn.on { background:rgba(91,209,165,.14); color:var(--accent); font-weight:600; }
  .addrow input, .addrow select { background:var(--bg); color:var(--text); border:1px solid var(--line);
          border-radius:7px; padding:5px 9px; font-size:12.5px; }
  .addrow input.key { width:120px; font-family:ui-monospace,Consolas,monospace; }
  .addrow input.zh { width:110px; }
  .addrow input.num { width:64px; }
  .addrow input.text { flex:1; min-width:160px; }
  .group { margin-top:16px; }
  .group > .ghead { display:flex; align-items:center; gap:8px; margin-bottom:8px; }
  .group > .ghead .gname { font-weight:600; font-size:12.5px; letter-spacing:.3px; }
  .group > .ghead .grow { flex:1; }
  .muted { color:var(--dim); font-size:12px; }
  details.json { margin-top:22px; border-top:1px dashed var(--line); padding-top:12px; }
  details.json summary { cursor:pointer; color:var(--dim); font-size:12.5px; }
  details.json pre { background:var(--panel); border:1px solid var(--line); border-radius:8px;
          padding:12px; font:11.5px/1.5 ui-monospace,Consolas,monospace; max-height:280px;
          overflow:auto; white-space:pre-wrap; color:var(--dim); }
  .usage { margin-top:6px; }
  .usage table { border-collapse:collapse; width:100%; font-size:12px; }
  .usage th, .usage td { text-align:left; padding:4px 6px; border-bottom:1px solid var(--line); }
  .usage th { color:var(--dim); font-weight:500; }
  .empty { color:var(--dim); font-size:12.5px; padding:6px 0; }
</style>
</head>
<body>
<header>
  <h1>词库管理</h1>
  <span class="sub">只管理捏人原料（枚举词 / 场景 / 口头禅 / 素材池），不编辑具体人物 · 仅本机可用</span>
  <span class="grow"></span>
  <span id="applied" class="pill">加载中…</span>
</header>
<main>
  <aside id="nav"></aside>
  <section>
    <div id="msg" class="msg"></div>
    <div id="view"></div>
  </section>
</main>
<script>
/*__ADMIN_TOKEN__*/
window.__SE_ADMIN_TOKEN__ = window.__SE_ADMIN_TOKEN__ || sessionStorage.getItem("seAdminToken") || "";
"use strict";

// ---------- 中文标签（只影响展示，不影响存储的 key） ----------
const FIELD_ZH = {
  sentence_rhythm: ["句子节奏", "说话是短促、平稳、流畅还是跳跃"],
  directness: ["直接程度", "有话直说还是先绕一下"],
  humor_style: ["幽默风格", "冷幽默 / 讽刺 / 俏皮 / 荒诞"],
  emotional_expression: ["情绪外露", "情绪是收着还是写在脸上"],
  vocabulary_style: ["用词风格", "朴素 / 精准 / 文雅 / 街头 / 专业"],
  slang_usage: ["俚语使用", "口语化程度"],
  emoji_usage: ["表情符号", "是否用 emoji、多不多"],
  verbosity: ["回复长度", "一句话 / 短 / 中 / 长"],
  conversation_initiative: ["主动性", "会不会主动起话题"],
  self_disclosure: ["自我暴露", "愿不愿意讲自己的事"],
  social_distance: ["社交距离", "疏远 / 中性 / 熟络 / 亲密"],
  disagreement_style: ["反对方式", "回避 / 缓和 / 直说 / 挑战"],
  topic_transition: ["话题转换", "留在原题 / 自然转 / 突然转 / 回扣"]
};
const VALUE_ZH = {
  clipped:"短促", measured:"平稳", flowing:"流畅", fragmented:"跳跃",
  blunt:"直白", direct:"直接", diplomatic:"委婉", indirect:"含蓄",
  none:"不用", dry:"冷幽默", sarcastic:"讽刺", playful:"俏皮", absurd:"荒诞",
  reserved:"克制", warm:"温暖", expressive:"外放", volatile:"情绪化",
  plain:"朴素", precise:"精准", literary:"文雅", street:"街头", technical:"专业",
  rare:"少", moderate:"适中", heavy:"多", never:"不用", sometimes:"有时", often:"常用",
  one_line:"一句话", short:"短", medium:"中", long:"长",
  low:"低", medium_init:"中", high:"高",
  guarded:"戒备", open:"开放", neutral:"中性", familiar:"熟络", intimate:"亲密",
  distant:"疏远", avoid:"回避", soften:"缓和", state_plainly:"直说", challenge:"挑战",
  stay:"留在原题", gentle_shift:"自然转", abrupt_shift:"突然转", callback:"回扣"
};
const LANG_ZH = { en:"英语", ja:"日语", ko:"韩语", fr:"法语", de:"德语",
                  es:"西班牙语", it:"意大利语", pt:"葡萄牙语", zh:"中文" };
const DESC = {
  events:"AI 动态由「事件」驱动：每天世界会产生日常事件和故事性事件，事件完成后按价值档和分享意愿发成动态。这里配置每类事件的价值档（谁先出现在信息流）、来源（规则/叙事）、分享权重和是否公开具体内容。",
  rhythm:"这些参数决定世界活不活跃：每天多少事件、多少条动态、评论概率、多久跑一轮。改完保存即生效（tick 两项需重启）。",

  dna:"每个字段一张卡片；取值就是标签。点「启用/停用」控制新角色是否还能分到这个取值（已有角色不受影响），拖动滑杆调整它对行为的影响强度（0..1），点 ★ 设为该字段的默认值。",
  scenes:"每个场景代表一类对话情境（寒暄、分享生活、安慰、意见不同……）。场景越多，示例语料覆盖越全；至少 8 个。",
  phrases:"禁用词会注入每个角色的 prompt（红线，越少越精确）；口头禅按语言分池，供生成新角色时参考。输入框回车即可添加，点 × 删除。",
  world:"名字（按语言，带性别/国家）、职业、城市（按国家分组）、兴趣。池子已接入真实捏人：有 LLM 时把池子写进 prompt 作为取材边界，没有 LLM 时按池子直接取；名字的性别/国家会和城市分组对齐，避免「英国名字 + 德国城市」。点「预览捏人」看效果。"
};
const NAV_ZH = { dna:"表达 DNA", scenes:"示例场景", phrases:"禁用词与口头禅", events:"AI 事件与动态", rhythm:"世界节奏", world:"捏人素材池" };
const NAV_DESC = { dna:"枚举值 + 行为数值", scenes:"12 类对话情境", phrases:"红线词 + 各语言口头禅", events:"事件类型 / 价值档 / 分享权重", rhythm:"产出频率 / 配额 / 惩罚", world:"名字 / 职业 / 城市 / 兴趣" };

const state = { name:null, libs:{}, items:[], usage:null, issues:{} };
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => { const n=document.createElement(tag); if(cls) n.className=cls;
  if(text!==undefined) n.textContent=text; return n; };

function show(kind, text){ const m=$("msg"); m.className="msg show "+kind; m.textContent=text; }
function hideMsg(){ $("msg").className="msg"; }

async function api(method, url, body){
  const headers = {"Content-Type":"application/json"};
  const token = window.__SE_ADMIN_TOKEN__ || sessionStorage.getItem("seAdminToken") || "";
  if(token) headers["X-Admin-Token"] = token;
  const res = await fetch(url, { method, headers,
    body: body ? JSON.stringify(body) : undefined });
  const data = await res.json().catch(()=>({}));
  if(!res.ok) throw new Error(data.detail || ("HTTP "+res.status));
  return data;
}

// ---------- 侧栏 ----------
function renderNav(){
  const nav = $("nav"); nav.innerHTML = "";
  for(const item of state.items){
    const n = el("div", "navitem" + (item.name===state.name ? " active":""));
    const head = el("div","n");
    const dot = el("span","dot " + (item.exists ? (item.ok?"ok":"bad") : "miss"));
    head.appendChild(dot); head.appendChild(el("span",null, NAV_ZH[item.name] || item.name));
    n.appendChild(head);
    n.appendChild(el("div","s", NAV_DESC[item.name] || item.title));
    n.onclick = () => openLib(item.name);
    nav.appendChild(n);
  }
  // 模型与密钥（不是词库，固定入口）
  const cfgItem = el("div", "navitem" + (state.name === "config" ? " active" : ""));
  const cfgHead = el("div","n");
  cfgHead.appendChild(el("span","dot ok"));
  cfgHead.appendChild(el("span",null,"模型与密钥"));
  cfgItem.appendChild(cfgHead);
  cfgItem.appendChild(el("div","s","LLM / 语音识别 / 语音合成"));
  cfgItem.onclick = () => openConfig();
  nav.appendChild(cfgItem);

  // Fish 音色池（固定入口）
  const vpItem = el("div", "navitem" + (state.name === "voices" ? " active" : ""));
  const vpHead = el("div","n");
  vpHead.appendChild(el("span","dot " + (state.voicePoolOk ? "ok" : "miss")));
  vpHead.appendChild(el("span",null,"音色池"));
  vpItem.appendChild(vpHead);
  vpItem.appendChild(el("div","s","Fish 音色 · 检测 / 素材池 / 试听"));
  vpItem.onclick = () => openVoicePool();
  nav.appendChild(vpItem);

  // 角色工坊（固定入口）
  const psItem = el("div", "navitem" + (state.name === "personas" ? " active" : ""));
  const psHead = el("div","n");
  psHead.appendChild(el("span","dot " + (state.personaOk ? "ok" : "miss")));
  psHead.appendChild(el("span",null,"角色工坊"));
  psItem.appendChild(psHead);
  psItem.appendChild(el("div","s","全量重捏 · 指定角色属性"));
  psItem.onclick = () => openPersonaStudio();
  nav.appendChild(psItem);

  // 图片库（固定入口）
  const mdItem = el("div", "navitem" + (state.name === "media" ? " active" : ""));
  const mdHead = el("div","n");
  mdHead.appendChild(el("span","dot ok"));
  mdHead.appendChild(el("span",null,"图片库"));
  mdItem.appendChild(mdHead);
  mdItem.appendChild(el("div","s","搜图 · 下载入库 · 按兴趣配图"));
  mdItem.onclick = () => openMediaLibrary();
  nav.appendChild(mdItem);

  // APK 发布（不是词库，固定入口）
  const relItem = el("div", "navitem" + (state.name === "release" ? " active" : ""));
  const relHead = el("div","n");
  relHead.appendChild(el("span","dot " + (state.releaseOk ? "ok" : "miss")));
  relHead.appendChild(el("span",null,"APK 发布"));
  relItem.appendChild(relHead);
  relItem.appendChild(el("div","s","在线更新 · 版本 / 安装包 / 回滚"));
  relItem.onclick = () => openRelease();
  nav.appendChild(relItem);

  if(state.usage){
    const box = el("div","usage");
    box.appendChild(el("div","muted","使用统计：" + state.usage.personas + " 个角色有 DNA"));
    nav.appendChild(box);
  }
}

// ---------- 公共小块 ----------
function chipBase(){
  return el("div","chip");
}
function toggleChip(chip, on){
  chip.classList.toggle("off", !on);
}

// ---------- DNA ----------
function renderDNA(lib){
  const wrap = el("div");

  const grid = el("div","grid");
  const fields = (lib.data && lib.data.fields) || {};
  const usageCounts = (state.usage && state.usage.dna) || {};

  for(const [field, spec] of Object.entries(fields)){
    const [zh, desc] = FIELD_ZH[field] || [field, ""];
    const card = el("div","card");
    const head = el("div","head");
    head.appendChild(el("span","zh",zh));
    head.appendChild(el("span","code",field));
    head.appendChild(el("span","grow"));
    card.appendChild(head);
    if(desc) card.appendChild(el("div","muted",desc));

    const chips = el("div","vals"); chips.style.marginTop = "9px";
    const values = spec.values || {};
    for(const [value, meta] of Object.entries(values)){
      const on = meta.enabled !== false;
      const c = el("div","val" + (on ? "" : " off"));
      const top = el("div","vtop");
      top.appendChild(el("span","vzh", VALUE_ZH[value] || value));
      top.appendChild(el("span","vkey", value));
      top.appendChild(el("span","grow"));
      const n = (usageCounts[field] && usageCounts[field][value]) || 0;
      if(n) top.appendChild(el("span","use", n + " 人"));
      const tg = el("button","toggle" + (on ? " on" : ""), on ? "启用" : "停用");
      tg.title = on ? "点击停用（新角色不再分配，已有角色不受影响）" : "点击重新启用";
      tg.onclick = () => { meta.enabled = !on; render(); };
      top.appendChild(tg);
      c.appendChild(top);

      const bottom = el("div","vbottom");
      if(meta.trait !== undefined){
        const slider = document.createElement("input");
        slider.type = "range"; slider.min = "0"; slider.max = "1"; slider.step = "0.01";
        slider.value = String(meta.trait);
        const num = el("span","tnum", Number(meta.trait).toFixed(2));
        slider.oninput = () => { meta.trait = Number(slider.value); num.textContent = Number(meta.trait).toFixed(2); };
        bottom.appendChild(slider);
        bottom.appendChild(num);
      }else{
        bottom.appendChild(el("span","use","不参与数值映射"));
      }
      const star = el("button","star" + (spec.default===value ? " on":""), spec.default===value ? "★" : "☆");
      star.title = "设为该字段默认值";
      star.onclick = () => { spec.default = value; render(); };
      bottom.appendChild(star);
      c.appendChild(bottom);
      chips.appendChild(c);
    }
    card.appendChild(chips);

    // 新增取值
    const add = el("div","addrow");
    const key = el("input","key"); key.placeholder = "key，如 long";
    const zhIn = el("input","zh"); zhIn.placeholder = "中文名，如 长";
    const num = el("input","num"); num.type = "number"; num.step="0.01"; num.min="0"; num.max="1";
    num.placeholder = "0..1";
    const btn = el("button","mini primary","＋ 新增取值");
    btn.onclick = () => {
      const k = key.value.trim();
      if(!k) return show("bad","请先填 key");
      if(/[^a-z0-9_]/i.test(k)) return show("bad","key 只能包含字母、数字、下划线");
      if((spec.values||{})[k]) return show("bad","该取值已存在：" + k);
      const raw = num.value.trim();
      const meta = { enabled:true };
      if(raw !== ""){
        const v = Number(raw);
        if(isNaN(v) || v<0 || v>1) return show("bad","数值必须在 0..1");
        meta.trait = Math.round(v*100)/100;
      }
      spec.values = spec.values || {};
      spec.values[k] = meta;
      if(!spec.default) spec.default = k;
      render();
      show("warn","已添加 " + k + "（记得点右上角保存并生效）");
    };
    add.appendChild(key); add.appendChild(zhIn); add.appendChild(num); add.appendChild(btn);
    card.appendChild(add);
    grid.appendChild(card);
  }
  wrap.appendChild(grid);
  wrap.appendChild(jsonPreview(lib));
  return wrap;
}

// ---------- 场景 ----------
function renderScenes(lib){
  const wrap = el("div");

  const card = el("div","card");
  const chips = el("div","chips");
  const list = (lib.data && lib.data.scenes) || [];
  list.forEach((item, idx) => {
    const key = typeof item === "string" ? item : item.key;
    const c = chipBase();
    c.appendChild(el("span","vzh", key));
    const x = el("button","x","×"); x.title="删除场景";
    x.onclick = () => { list.splice(idx,1); render(); };
    c.appendChild(x);
    chips.appendChild(c);
  });
  if(!list.length) chips.appendChild(el("div","empty","还没有场景，先加几个吧"));
  card.appendChild(chips);

  const add = el("div","addrow");
  const inp = el("input","text"); inp.placeholder = "新场景 key，例如 weekend_plan";
  const btn = el("button","mini primary","＋ 添加场景");
  btn.onclick = () => {
    const k = inp.value.trim();
    if(!k) return show("bad","请输入场景 key");
    list.push({ key: k });
    render();
    show("warn","已添加场景 " + k + "（保存后生效）");
  };
  add.appendChild(inp); add.appendChild(btn);
  card.appendChild(add);
  wrap.appendChild(card);
  wrap.appendChild(jsonPreview(lib));
  return wrap;
}

// ---------- 口头禅 / 禁用词 ----------
function renderPhrases(lib){
  const wrap = el("div");

  // 全局禁用词
  const bannedCard = el("div","card");
  bannedCard.appendChild(el("div","head")).appendChild(el("span","zh","全局禁用词（红线）"));
  const bchips = el("div","chips");
  const banned = lib.data.banned_global = lib.data.banned_global || [];
  banned.forEach((word, i) => {
    const c = chipBase();
    c.appendChild(el("span","vzh", word));
    const x = el("button","x","×");
    x.onclick = () => { banned.splice(i,1); render(); };
    c.appendChild(x); bchips.appendChild(c);
  });
  bannedCard.appendChild(bchips);
  const badd = el("div","addrow");
  const binp = el("input","text"); binp.placeholder = "新增禁用词，例如 as an AI";
  const bbtn = el("button","mini primary","＋ 添加");
  bbtn.onclick = () => { const v=binp.value.trim(); if(!v) return; banned.push(v); render(); };
  binp.onkeydown = (e) => { if(e.key==="Enter") bbtn.onclick(); };
  badd.appendChild(binp); badd.appendChild(bbtn);
  bannedCard.appendChild(badd);
  wrap.appendChild(bannedCard);

  // 口头禅池
  const pools = lib.data.signature_pools = lib.data.signature_pools || {};
  const poolCard = el("div","card"); poolCard.style.marginTop = "14px";
  poolCard.appendChild(el("div","head")).appendChild(el("span","zh","口头禅池（按语言）"));
  for(const [lang, items] of Object.entries(pools)){
    const g = el("div","group");
    const gh = el("div","ghead");
    gh.appendChild(el("span","gname", (LANG_ZH[lang]||lang) + " · " + lang));
    gh.appendChild(el("span","muted", items.length + " 条"));
    gh.appendChild(el("div","grow"));
    g.appendChild(gh);
    const chips = el("div","chips");
    items.forEach((p, i) => {
      const c = chipBase();
      c.appendChild(el("span","vzh", p));
      const x = el("button","x","×");
      x.onclick = () => { items.splice(i,1); render(); };
      c.appendChild(x);
      chips.appendChild(c);
    });
    g.appendChild(chips);
    const add = el("div","addrow");
    const inp = el("input","text"); inp.placeholder = "新增口头禅（"+(LANG_ZH[lang]||lang)+"）";
    const btn = el("button","mini primary","＋ 添加");
    btn.onclick = () => { const v=inp.value.trim(); if(!v) return; items.push(v); render(); };
    inp.onkeydown = (e) => { if(e.key==="Enter") btn.onclick(); };
    add.appendChild(inp); add.appendChild(btn);
    g.appendChild(add);
    poolCard.appendChild(g);
  }
  // 新增语言池
  const addLang = el("div","addrow");
  const langInp = el("input","key"); langInp.placeholder = "语言码，如 fr";
  const langBtn = el("button","mini","＋ 新增语言池");
  langBtn.onclick = () => {
    const k = langInp.value.trim().toLowerCase();
    if(!k) return;
    if(pools[k]) return show("bad","该语言已存在");
    pools[k] = [];
    render();
  };
  addLang.appendChild(langInp); addLang.appendChild(langBtn);
  poolCard.appendChild(addLang);
  wrap.appendChild(poolCard);
  wrap.appendChild(jsonPreview(lib));
  return wrap;
}

// ---------- 捏人素材池 ----------
function renderWorld(lib){
  const wrap = el("div");

  const groups = [
    ["names","名字（按语言，带性别/国家）","语言码，如 en"],
    ["occupations","职业","分组，如 any"],
    ["cities","城市（按国家分组）","国家名，如 Germany"],
    ["interests","兴趣","分组，如 any"],
  ];
  for(const [key, title, hint] of groups){
    const card = el("div","card"); card.style.marginBottom = "14px";
    card.appendChild(el("div","head")).appendChild(el("span","zh", title));
    const pools = lib.data[key] = lib.data[key] || {};
    for(const [group, items] of Object.entries(pools)){
      const g = el("div","group");
      const gh = el("div","ghead");
      gh.appendChild(el("span","gname", (key==="names" ? (LANG_ZH[group]||group) : group)));
      gh.appendChild(el("span","muted", items.length + " 条"));
      gh.appendChild(el("div","grow"));
      if(Object.keys(pools).length > 1){
        const del = el("button","mini danger","删除该分组");
        del.onclick = () => { if(confirm("删除分组 "+group+" ？")) { delete pools[group]; render(); } };
        gh.appendChild(del);
      }
      g.appendChild(gh);
      const chips = el("div","chips");
      items.forEach((v, i) => {
        const c = chipBase();
        if(key === "names"){
          const entry = (typeof v === "string") ? { name: v, gender: "", country: "" } : v;
          c.appendChild(el("span","vzh", entry.name || String(v)));
          if(entry.gender) c.appendChild(el("span","use", entry.gender === "male" ? "♂" : "♀"));
          if(entry.country) c.appendChild(el("span","use", "· " + entry.country));
        }else{
          c.appendChild(el("span","vzh", v));
        }
        const x = el("button","x","×");
        x.onclick = () => { items.splice(i,1); render(); };
        c.appendChild(x); chips.appendChild(c);
      });
      if(!items.length) chips.appendChild(el("div","empty","空"));
      g.appendChild(chips);
      const add = el("div","addrow");
      const inp = el("input","text"); inp.placeholder = key === "names" ? "名字，如 Jonas" : "新增一条";
      let genderSel = null, countryInp = null;
      if(key === "names"){
        genderSel = document.createElement("select");
        genderSel.className = "langsel";
        [["", "性别"], ["female", "女"], ["male", "男"]].forEach(([value, label]) => {
          const opt = document.createElement("option"); opt.value = value; opt.textContent = label;
          genderSel.appendChild(opt);
        });
        countryInp = el("input","text");
        countryInp.placeholder = "国家，如 Germany";
        countryInp.style.width = "140px";
      }
      const btn = el("button","mini primary","＋ 添加");
      btn.onclick = () => {
        const v = inp.value.trim();
        if(!v) return;
        if(key === "names"){
          items.push({ name: v, gender: genderSel.value, country: countryInp.value.trim() });
        }else{
          items.push(v);
        }
        render();
      };
      inp.onkeydown = (e) => { if(e.key==="Enter") btn.onclick(); };
      add.appendChild(inp);
      if(genderSel) add.appendChild(genderSel);
      if(countryInp) add.appendChild(countryInp);
      add.appendChild(btn);
      g.appendChild(add);
      card.appendChild(g);
    }
    const addGroup = el("div","addrow");
    const ginp = el("input","key"); ginp.placeholder = hint;
    const gbtn = el("button","mini","＋ 新增分组");
    gbtn.onclick = () => { const k=ginp.value.trim(); if(!k) return; pools[k]=[]; render(); };
    addGroup.appendChild(ginp); addGroup.appendChild(gbtn);
    card.appendChild(addGroup);
    wrap.appendChild(card);
  }
  wrap.appendChild(jsonPreview(lib));
  return wrap;
}


// ---------- AI 事件与动态 ----------
const EVENT_ZH = { achievement:"个人成就", conflict:"冲突", personal_milestone:"个人里程碑",
  relationship:"关系变化", social:"社交", entertainment:"娱乐", travel:"旅行",
  work:"工作", study:"学习", unexpected_event:"意外", meal:"吃饭", daily_routine:"日常" };
const SOURCE_ZH = { rule:"规则事件", narrative:"叙事事件", both:"两者" };

function renderEvents(lib){
  const wrap = el("div");
  const grid = el("div","grid");
  const types = lib.data.types = lib.data.types || {};
  for(const [key, spec] of Object.entries(types)){
    const on = spec.enabled !== false;
    const card = el("div","card" + (on ? "" : " off"));
    const head = el("div","head");
    head.appendChild(el("span","zh", spec.zh || EVENT_ZH[key] || key));
    head.appendChild(el("span","code", key));
    head.appendChild(el("span","grow"));
    const tg = el("button","mini" + (on ? " primary" : ""), on ? "启用" : "已停用");
    tg.title = on ? "点击停用（不再产生新事件，历史保留）" : "点击重新启用";
    tg.onclick = () => { spec.enabled = !on; render(); };
    head.appendChild(tg);
    card.appendChild(head);

    // 价值档（0=最低，3=最有故事）
    const tierRow = el("div","kvrow");
    tierRow.appendChild(el("span","klabel","价值档"));
    const tierWrap = el("div","seg");
    [0,1,2,3].forEach((tier) => {
      const b = el("button","segbtn" + (Number(spec.value_tier) === tier ? " on" : ""), tier === 0 ? "低" : "★".repeat(tier));
      b.title = "信息流优先展示价值高的：" + tier;
      b.onclick = () => { spec.value_tier = tier; render(); };
      tierWrap.appendChild(b);
    });
    tierRow.appendChild(tierWrap);
    card.appendChild(tierRow);

    // 来源
    const srcRow = el("div","kvrow");
    srcRow.appendChild(el("span","klabel","来源"));
    const srcWrap = el("div","seg");
    ["rule","narrative","both"].forEach((src) => {
      const b = el("button","segbtn" + (spec.source === src ? " on" : ""), SOURCE_ZH[src]);
      b.title = src === "rule" ? "按作息自动产生（不需要 LLM）" : (src === "narrative" ? "由 LLM 写故事性事件" : "两者都会产生");
      b.onclick = () => { spec.source = src; render(); };
      srcWrap.appendChild(b);
    });
    srcRow.appendChild(srcWrap);
    card.appendChild(srcRow);

    // 分享权重（倍率 0..2）
    const wRow = el("div","kvrow");
    wRow.appendChild(el("span","klabel","分享权重"));
    const slider = document.createElement("input");
    slider.type = "range"; slider.min = "0"; slider.max = "2"; slider.step = "0.01";
    slider.value = String(spec.share_weight ?? 0.7);
    const num = el("span","tnum", Number(spec.share_weight ?? 0.7).toFixed(2));
    slider.oninput = () => { spec.share_weight = Number(slider.value); num.textContent = Number(spec.share_weight).toFixed(2); };
    wRow.appendChild(slider);
    wRow.appendChild(num);
    card.appendChild(wRow);

    // 是否公开具体内容
    const titleRow = el("div","kvrow");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = spec.show_title !== false;
    cb.onchange = () => { spec.show_title = cb.checked; };
    titleRow.appendChild(cb);
    titleRow.appendChild(el("span","klabel","对用户展示具体内容"));
    titleRow.appendChild(el("span","grow"));
    titleRow.appendChild(el("span","use", "红线：冲突类默认不展示"));
    card.appendChild(titleRow);

    // 用户可见 tag
    const tagRow = el("div","kvrow");
    tagRow.appendChild(el("span","klabel","可见 tag"));
    const tagInp = el("input","text");
    tagInp.value = (spec.tags || []).join(", ");
    tagInp.placeholder = "逗号分隔，例如 daily, mood";
    tagInp.onchange = () => {
      spec.tags = tagInp.value.split(",").map((s) => s.trim()).filter(Boolean);
    };
    tagRow.appendChild(tagInp);
    card.appendChild(tagRow);
    grid.appendChild(card);
  }
  wrap.appendChild(grid);
  const note = el("p","muted","新增事件类型需要代码支持（叙事 prompt 与校验要认识它），所以这里只能启用/停用和调参；停用后不再产生新事件，历史事件保留。");
  wrap.appendChild(note);
  wrap.appendChild(jsonPreview(lib));
  return wrap;
}

// ---------- 世界节奏 ----------
const RHYTHM_META = {
  rule_events_per_day:      ["每天每人规则事件", "按作息自动产生，1–8 条", 1, 8, 1, ""],
  narrative_events_per_day: ["每天每人叙事事件", "0 = 关闭 LLM 叙事（省钱），最多 6", 0, 6, 1, ""],
  event_posts_per_day:      ["每日事件动态配额", "全体总闸，1–30 条", 1, 30, 1, ""],
  tension_penalty_step:     ["张力惩罚步长", "越大越记仇，0–0.5", 0, 0.5, 0.01, ""],
  tension_penalty_floor:    ["张力惩罚下限", "最低保留多少互动意愿，0–0.8", 0, 0.8, 0.01, ""],
  comment_backfill_hours:   ["评论补位窗口", "只给最近这么多小时的动态补评论", 0, 24, 0.5, " 小时"],
  life_tick_s:              ["生活事件 tick", "多久跑一轮生活事件", 60, 3600, 30, " 秒（重启生效）"],
  posts_tick_s:             ["事件动态 tick", "多久扫一轮待发事件", 60, 3600, 30, " 秒（重启生效）"],
  posts_max_events:         ["每轮最多处理事件", "单轮批量上限", 1, 20, 1, " 条"],
  posts_since_hours:        ["只看最近几小时事件", "避免把很旧的事件翻出来发", 1, 48, 1, " 小时"]
};

function renderRhythm(lib){
  const wrap = el("div");
  const params = lib.data.params = lib.data.params || {};
  const card = el("div","card");
  for(const [key, meta] of Object.entries(RHYTHM_META)){
    const [label, hint, min, max, step, unit] = meta;
    const row = el("div","kvrow");
    const left = el("div","kvleft");
    left.appendChild(el("div","klabel", label));
    left.appendChild(el("div","use", hint));
    row.appendChild(left);
    const slider = document.createElement("input");
    slider.type = "range"; slider.min = String(min); slider.max = String(max); slider.step = String(step);
    const value = params[key] !== undefined ? Number(params[key]) : min;
    slider.value = String(value);
    const num = el("input","knum");
    num.value = String(value) + unit;
    num.readOnly = true;
    slider.oninput = () => {
      const v = Number(slider.value);
      params[key] = (step >= 1 ? Math.round(v) : Math.round(v * 100) / 100);
      num.value = String(params[key]) + unit;
    };
    row.appendChild(slider);
    row.appendChild(num);
    card.appendChild(row);
  }
  wrap.appendChild(card);
  const note = el("p","muted","改完点右上「保存并生效」；除 tick 外都立即生效。世界会按新参数继续自转，不会补发历史缺失的事件。");
  wrap.appendChild(note);
  wrap.appendChild(jsonPreview(lib));
  return wrap;
}

// ---------- 模拟今天 ----------
async function previewPersonas(language){
  const lang = (language || "en").trim() || "en";
  try{
    const res = await api("GET", "/api/admin/preview-personas?count=4&language=" + encodeURIComponent(lang));
    if(res.error){ show("bad","预览失败：" + res.error); return; }
    const pool = res.pools || {};
    let out = `【素材池 ${lang}】名字 ${pool.names || 0} · 职业 ${pool.occupations || 0} · 城市 ${pool.cities || 0} · 兴趣 ${pool.interests || 0}\\n`;
    for(const p of (res.personas || [])){
      out += `· ${p.nickname}（${p.gender === "male" ? "男" : "女"}，${p.age}）｜${p.city}｜${p.occupation}｜#${(p.interests || []).join(" #")}\\n`;
    }
    if((res.warnings || []).length){
      out += "\\n⚠ " + res.warnings.join("\\n⚠ ");
    }
    out += "\\n（预览不写库、不调模型；保存后下次捏人就会用新素材）";
    show("ok", out);
  }catch(e){ show("bad","预览失败：" + e.message); }
}

async function simulateToday(){
  try{
    const res = await api("GET","/api/admin/simulate");
    const t = res.today || {}, p = res.pending || {}, n = res.next_day || {}, w = res.world || {};
    let out = "【世界现状】人物 " + (w.personas ?? "?") + " 个 · 事件类型 " + (res.types && res.types.enabled || "?") + " 个启用\\n";
    out += "【今天】事件 " + JSON.stringify(t.events || {}) + " · 动态 " + (t.posts ?? 0) + " 条 · 评论 " + (t.comments ?? 0) + " 条\\n";
    out += "【待发】已发生但还没发动态的事件 " + (p.eligible_events ?? 0) + " 个，平均分享意愿 " + (p.avg_share_prob ?? "?") + "\\n";
    out += "　→ 按当前配额，预计还能发 " + (p.expected_new_posts ?? 0) + " 条动态、" + (p.expected_new_comments ?? 0) + " 条评论\\n";
    out += "【若按当前参数跑一天】规则事件约 " + (n.rule_events_est ?? "?") + " 条 + 叙事事件 " + (n.narrative_events_est ?? "?") + " 条，动态配额 " + (n.posts_quota ?? "?") + " 条";
    show("ok", out);
  }catch(e){ show("bad","模拟失败：" + e.message); }
}


// ---------- 模型与密钥 ----------
const PROVIDERS = { llm: "对话模型（LLM）", stt: "语音识别（STT）", tts: "语音合成（TTS）" };

function cfgField(label, value, opts){
  const row = el("div","kvrow");
  const left = el("div","kvleft");
  left.appendChild(el("div","klabel", label));
  if(opts && opts.hint) left.appendChild(el("div","use", opts.hint));
  row.appendChild(left);
  const input = document.createElement("input");
  input.className = "cfginput";
  input.type = (opts && opts.type) || "text";
  if(opts && opts.placeholder) input.placeholder = opts.placeholder;
  input.value = value === undefined || value === null ? "" : String(value);
  input.dataset.field = label;
  row.appendChild(input);
  row._input = input;
  return row;
}

function cfgCard(title, subtitle, rows, target, status){
  const card = el("div","card");
  const head = el("div","head");
  head.appendChild(el("span","zh", title));
  head.appendChild(el("span","grow"));
  card.appendChild(head);
  if(subtitle) card.appendChild(el("div","muted", subtitle));
  const body = el("div"); body.style.marginTop = "8px";
  const inputs = {};
  rows.forEach((row) => {
    body.appendChild(row);
    inputs[row.dataset.field || (row._input && row._input.dataset.field)] = row._input;
  });
  card.appendChild(body);
  if(status) card.appendChild(el("div","use", status));

  const bar = el("div","addrow");
  const testBtn = el("button","mini","测试连接");
  const saveBtn = el("button","mini primary","保存并生效");
  const forceCb = document.createElement("input"); forceCb.type = "checkbox";
  forceCb.title = "测试失败也保存（离线部署/内网模型可用）";
  bar.appendChild(testBtn); bar.appendChild(saveBtn);
  bar.appendChild(forceCb); bar.appendChild(el("span","use","忽略测试强制保存"));
  card.appendChild(bar);

  const collect = () => {
    const out = {};
    for(const [field, input] of Object.entries(inputs)){
      if(!input) continue;
      const value = input.value.trim();
      if(input.type === "checkbox"){ out[field] = input.checked; continue; }
      if(value === "") continue;           // 空 = 不改
      if(field === "__skip") continue;
      out[input.dataset.field || field] = value;
    }
    if(forceCb.checked) out.force = true;
    return out;
  };
  testBtn.onclick = async () => {
    try{
      const res = await api("POST", `/api/admin/config/${target}/test`);
      show(res.ok ? "ok" : "bad", `【${title} 测试】${res.message || (res.ok ? "通过" : "失败")}`);
    }catch(e){ show("bad","测试失败：" + e.message); }
  };
  saveBtn.onclick = async () => {
    const payload = collect();
    if(!Object.keys(payload).filter((k) => k !== "force").length){
      show("warn","没有改动（留空表示不修改）"); return;
    }
    try{
      const res = await api("PUT", `/api/admin/config/${target}`, payload);
      if(res.ok) show("ok", `【${title}】已保存并生效 ✓`);
      else show("bad", `【${title}】保存被拒绝：` + (res.issues || [res.message || "未知"]).join(" • "));
      if(res.ok) await openConfig();
    }catch(e){ show("bad","保存失败：" + e.message); }
  };
  return card;
}

function renderConfigView(){
  const cfg = state.config || {};
  const view = $("view");
  view.innerHTML = "";
  const bar = el("div","bar");
  const t = el("div");
  t.appendChild(el("h2",null,"模型与密钥"));
  t.appendChild(el("p",null,"这里的配置只在本机管理端可见，客户端没有任何 key 输入框。保存前会**先做连通测试**，测试失败默认不落盘（可勾选强制保存）——避免「改坏配置导致客户端用不了」。"));
  bar.appendChild(t); bar.appendChild(el("div","grow"));
  const refresh = el("button",null,"刷新状态");
  refresh.onclick = () => openConfig();
  bar.appendChild(refresh);
  view.appendChild(bar);

  const grid = el("div","grid");
  const llm = cfg.llm || {};
  const stt = cfg.stt || {};
  const tts = cfg.tts || {};
  const fish = cfg.fish || {};
  const dg = cfg.deepgram || {};

  grid.appendChild(cfgCard(
    PROVIDERS.llm,
    "对话模型：base_url / model / api_key。保存前会真的发一次极小请求验证。",
    [
      cfgField("base_url", llm.base_url, {placeholder:"https://api.deepseek.com/v1"}),
      cfgField("model", llm.model, {placeholder:"deepseek-chat"}),
      cfgField("api_key", "", {type:"password", placeholder: llm.masked_key || "留空 = 不修改"}),
      cfgField("suggestion_model", llm.suggestion_model, {placeholder:"（可选）更便宜的建议模型"}),
    ],
    "llm",
    `当前：${llm.configured ? "已配置" : "未配置"} · key ${llm.masked_key || "未设置"}`,
  ));

  grid.appendChild(cfgCard(
    PROVIDERS.stt,
    "语音识别：siliconflow 云端需要 key；local 用本地 Whisper；auto 自动选。",
    [
      cfgField("provider", stt.effective_provider || stt.provider || "auto", {hint:"auto / local / siliconflow"}),
      cfgField("base_url", stt.base_url, {placeholder:"https://api.siliconflow.cn/v1"}),
      cfgField("model", stt.model, {placeholder:"FunAudioLLM/SenseVoiceSmall"}),
      cfgField("api_key", "", {type:"password", placeholder: stt.api_key_masked || "留空 = 不修改"}),
    ],
    "stt",
    `当前：${stt.provider || "auto"} · key ${stt.api_key_masked || "未设置"}`,
  ));

  grid.appendChild(cfgCard(
    PROVIDERS.tts,
    "语音合成：edge 免费（无 key）；fish 需要 API Key。保存前会探测 Fish 连通性。",
    [
      cfgField("provider", tts.provider || "edge", {hint:"edge / fish"}),
      cfgField("fish_api_key", "", {type:"password", placeholder: tts.fish_api_key || "留空 = 不修改"}),
      cfgField("fish_base_url", tts.fish_base_url, {placeholder:"https://api.fish.audio"}),
      cfgField("fish_model", tts.fish_model, {placeholder:"s2.1-pro-free"}),
      cfgField("fish_reference_id", tts.fish_reference_id, {placeholder:"（可选）默认参考音色 id"}),
    ],
    "tts",
    `当前：${tts.provider || "edge"} · Fish ${fish.reachable ? "可达" : "不可达"} · key ${tts.fish_api_key || "未设置"}`,
  ));

  grid.appendChild(cfgCard(
    "Deepgram · 通话流式识别",
    "通话模式的实时语音转文字用 Deepgram（不是语音合成）。不配也能用整段上传识别。",
    [
      cfgField("api_key", "", {type:"password", placeholder: dg.key_masked || "留空 = 不修改"}),
    ],
    "deepgram",
    `当前：${dg.available ? "已配置" : "未配置"} · 来源 ${dg.source || "none"} · key ${dg.key_masked || "未设置"}`,
  ));

  view.appendChild(grid);
  const note = el("p","muted","客户端已锁死配置入口：任何通过客户端改 key 的请求都会被拒绝（403）。需要临时放开调试时设 ALLOW_KEY_EDIT=1。");
  view.appendChild(note);
}



// ---------- Fish 音色池 ----------
const VP_LANGS = [["en","英语"],["zh","中文"],["ja","日语"],["ko","韩语"],["es","西班牙语"],["fr","法语"],["de","德语"],["pt","葡萄牙语"],["it","意大利语"],["ar","阿拉伯语"],["ru","俄语"]];
const VP_GENDERS = [["","不限"],["female","女声"],["male","男声"]];

function vpToken(){
  return window.__SE_ADMIN_TOKEN__ || sessionStorage.getItem("seAdminToken") || "";
}

function vpHeaders(extra){
  const h = Object.assign({ "Content-Type": "application/json" }, extra || {});
  const token = vpToken();
  if(token) h["X-Admin-Token"] = token;
  return h;
}

function vpSel(options){
  const s = document.createElement("select");
  s.className = "cfginput";
  options.forEach(([v,label]) => {
    const o = document.createElement("option");
    o.value = v; o.textContent = label;
    s.appendChild(o);
  });
  return s;
}

function vpInput(placeholder){
  const i = document.createElement("input");
  i.className = "cfginput";
  i.placeholder = placeholder || "";
  return i;
}

function vpMsg(kind, text){
  const box = $("vpMsg");
  if(!box){ alert(text); return; }
  box.hidden = false;
  box.className = "vpmsg " + kind;
  box.textContent = text;
}

function vpPlay(referenceId){
  const status = $("vpPlayStatus");
  if(status) status.textContent = "合成/加载中…";
  fetch("/api/admin/voice-pool/preview", {
    method: "POST",
    headers: vpHeaders(),
    body: JSON.stringify({ reference_id: referenceId }),
  })
    .then(async (res) => {
      if(!res.ok){
        let detail = "HTTP " + res.status;
        try { const data = await res.json(); detail = data.detail || detail; } catch(e){}
        throw new Error(detail);
      }
      return res.blob();
    })
    .then((blob) => {
      const audio = $("vpPlayer");
      audio.src = URL.createObjectURL(blob);
      audio.hidden = false;
      audio.play();
      if(status) status.textContent = "正在播放：" + referenceId;
    })
    .catch((e) => { if(status) status.textContent = "试听失败：" + e.message; });
}

async function vpValidate(){
  const input = $("vpRef");
  const rid = (input.value || "").trim();
  if(!rid){ vpMsg("warn","先填写 Fish 音色/model id"); return; }
  vpMsg("info","正在用该音色合成一句做检测…");
  try{
    const res = await fetch("/api/admin/voice-pool/validate", {
      method: "POST", headers: vpHeaders(), body: JSON.stringify({ reference_id: rid }),
    });
    const data = await res.json();
    if(!data.ok){ vpMsg("bad", "检测失败：" + (data.issues || ["未知"]).join("；")); return; }
    vpMsg("ok", "检测成功 ✓ 正在播放试听");
    vpPlay(rid);
  }catch(e){ vpMsg("bad","检测失败：" + e.message); }
}

async function vpAddCurrent(){
  const rid = ($("vpRef").value || "").trim();
  if(!rid){ vpMsg("warn","先填写 Fish 音色/model id"); return; }
  const name = ($("vpName").value || "").trim();
  const gender = $("vpGender").value;
  const language = $("vpLang").value;
  vpMsg("info","正在检测并加入素材池…");
  try{
    const res = await fetch("/api/admin/voice-pool/add", {
      method: "POST", headers: vpHeaders(),
      body: JSON.stringify({ reference_id: rid, name: name, gender: gender, language: language }),
    });
    const data = await res.json();
    if(!data.ok){ vpMsg("bad", "加入失败：" + (data.issues || ["未知"]).join("；")); return; }
    vpMsg("ok", "已加入素材池 ✓ 新捏人的人物会优先从这里挑音色");
    await openVoicePool();
  }catch(e){ vpMsg("bad","加入失败：" + e.message); }
}

async function vpAddFromModel(rid, title, langs, tags){
  const lang = (langs && langs.length) ? String(langs[0]).toLowerCase().slice(0,2) : "";
  const tagText = (tags || []).map(t => String(t).toLowerCase());
  const gender = tagText.includes("female") ? "female" : (tagText.includes("male") ? "male" : "");
  vpMsg("info","正在检测并加入：" + (title || rid));
  try{
    const res = await fetch("/api/admin/voice-pool/add", {
      method: "POST", headers: vpHeaders(),
      body: JSON.stringify({ reference_id: rid, name: title || rid.slice(0,12), gender: gender, language: lang, source: "fish-model" }),
    });
    const data = await res.json();
    if(!data.ok){ vpMsg("bad", "加入失败：" + (data.issues || ["未知"]).join("；")); return; }
    vpMsg("ok", "已加入素材池：" + (title || rid));
    await openVoicePool();
  }catch(e){ vpMsg("bad","加入失败：" + e.message); }
}

async function vpRemove(rid){
  if(!confirm("从素材池移除 " + rid + "？已分配给人物角色的声音不会变。")) return;
  try{
    const res = await fetch("/api/admin/voice-pool/remove", {
      method: "POST", headers: vpHeaders(), body: JSON.stringify({ reference_id: rid }),
    });
    const data = await res.json();
    if(!data.ok){ vpMsg("bad","移除失败"); return; }
    await openVoicePool();
  }catch(e){ vpMsg("bad","移除失败：" + e.message); }
}

async function vpLoadModels(){
  vpMsg("info","正在拉取 Fish 账号下的音色列表…");
  try{
    const res = await fetch("/api/admin/voice-pool/models?refresh=1", { headers: vpHeaders({}) });
    const data = await res.json();
    if(!data.ok){ vpMsg("bad","拉取失败：" + (data.issues || ["未知"]).join("；")); return; }
    state.fishModels = data.models || [];
    renderVoicePoolModels();
    vpMsg("ok", "已加载 " + state.fishModels.length + " 个 Fish 音色");
  }catch(e){ vpMsg("bad","拉取失败：" + e.message); }
}

function renderVoicePoolModels(){
  const box = $("vpModels");
  if(!box) return;
  box.innerHTML = "";
  const keyword = (($("vpSearch") || {}).value || "").trim().toLowerCase();
  const models = (state.fishModels || []).filter((m) => {
    if(!keyword) return true;
    return (String(m.title || "") + " " + String(m.id || "") + " " + (m.languages || []).join(",") + " " + (m.tags || []).join(",")).toLowerCase().includes(keyword);
  });
  if(!models.length){
    box.appendChild(el("p","muted", state.fishModels && state.fishModels.length ? "没有匹配的音色" : "点上面的「加载 Fish 全部音色」"));
    return;
  }
  models.slice(0, 600).forEach((m) => {
    const row = el("div","vprow");
    const info = el("div","vpinfo");
    const name = el("div","vpname", m.title || "(未命名)");
    const meta = el("div","vpmeta", m.id + " · " + ((m.languages || []).join("/") || "未知语言") + ((m.tags || []).length ? " · " + (m.tags || []).join(",") : ""));
    info.appendChild(name); info.appendChild(meta);
    row.appendChild(info);
    const actions = el("div","vpactions");
    const play = el("button","mini","试听");
    play.onclick = () => vpPlay(m.id);
    const add = el("button","mini primary","加入池");
    add.onclick = () => vpAddFromModel(m.id, m.title, m.languages, m.tags);
    actions.appendChild(play); actions.appendChild(add);
    row.appendChild(actions);
    box.appendChild(row);
  });
  if(models.length > 600){
    box.appendChild(el("p","muted","仅显示前 600 个，用过滤框缩小范围"));
  }
}

async function openVoicePool(){
  state.name = "voices";
  hideMsg();
  history.replaceState(null, "", "#voices");
  try{
    state.voicePool = await api("GET","/api/admin/voice-pool");
    state.voicePoolOk = (state.voicePool.voices || []).length > 0;
  }catch(e){
    show("bad","读取音色池失败：" + e.message);
    return;
  }
  render();
}

function renderVoicePoolView(){
  const st = state.voicePool || {};
  const voices = st.voices || [];
  const fish = st.fish || {};
  const view = $("view");
  view.innerHTML = "";

  const bar = el("div","bar");
  const t = el("div");
  t.appendChild(el("h2",null,"Fish 音色池"));
  t.appendChild(el("p",null,"手动输入 Fish 音色/model id → 真实合成一句检测 → 加入素材池；新捏的人物会优先从素材池挑音色。点「试听」可以播放。"));
  bar.appendChild(t); bar.appendChild(el("div","grow"));
  const stateText = el("span","mini", "Fish " + (fish.usable ? "可用" : (fish.configured ? "冷却中" : "未配置/非 fish")));
  bar.appendChild(stateText);
  const refresh = el("button",null,"刷新");
  refresh.onclick = () => openVoicePool();
  bar.appendChild(refresh);
  view.appendChild(bar);

  const msg = el("div","vpmsg");
  msg.id = "vpMsg"; msg.hidden = true;
  view.appendChild(msg);

  const player = document.createElement("audio");
  player.id = "vpPlayer"; player.controls = true; player.hidden = true;
  player.style.width = "100%"; player.style.margin = "6px 0";
  view.appendChild(player);
  const playStatus = el("p","muted");
  playStatus.id = "vpPlayStatus"; playStatus.textContent = "";
  view.appendChild(playStatus);

  // ---- 手动添加 ----
  const addCard = el("div","card");
  const ah = el("div","head"); ah.appendChild(el("span","zh","手动添加音色")); ah.appendChild(el("span","grow"));
  addCard.appendChild(ah);
  const mkrow = (label, node, hint) => {
    const row = el("div","kvrow");
    const left = el("div","kvleft");
    left.appendChild(el("div","klabel", label));
    if(hint) left.appendChild(el("div","use", hint));
    row.appendChild(left); row.appendChild(node); addCard.appendChild(row);
  };
  const ref = vpInput("Fish 模型 / reference_id，例如 7f92c1…");
  ref.id = "vpRef";
  mkrow("音色 id", ref, "Fish 平台音色模型 id（reference_id）");
  const name = vpInput("给这个音色起个名字（可选）");
  name.id = "vpName";
  mkrow("名称", name);
  const gender = vpSel(VP_GENDERS);
  gender.id = "vpGender";
  mkrow("性别", gender, "影响捏人时按性别匹配");
  const lang = vpSel(VP_LANGS);
  lang.id = "vpLang";
  mkrow("语言", lang, "影响捏人时按语言匹配");
  const btns = el("div","addrow");
  const validate = el("button","mini","检测并试听");
  validate.onclick = () => vpValidate();
  const add = el("button","mini primary","加入素材池");
  add.onclick = () => vpAddCurrent();
  btns.appendChild(validate); btns.appendChild(add);
  addCard.appendChild(btns);
  view.appendChild(addCard);

  // ---- 素材池 ----
  const poolCard = el("div","card");
  const ph = el("div","head");
  ph.appendChild(el("span","zh","素材池（已加入 " + voices.length + " 个）"));
  ph.appendChild(el("span","grow"));
  poolCard.appendChild(ph);
  if(!voices.length){
    poolCard.appendChild(el("p","muted","还没有加入音色。上面填 id 检测通过后加入，或从下面 Fish 全部音色里点「加入池」。"));
  } else {
    voices.forEach((v) => {
      const row = el("div","vprow");
      const info = el("div","vpinfo");
      info.appendChild(el("div","vpname", v.name || v.reference_id));
      const bits = [v.reference_id];
      if(v.language) bits.push("语言:" + v.language);
      if(v.gender) bits.push("性别:" + v.gender);
      bits.push("已分配:" + (v.assigned || 0) + " 人");
      info.appendChild(el("div","vpmeta", bits.join(" · ")));
      row.appendChild(info);
      const actions = el("div","vpactions");
      const play = el("button","mini","试听");
      play.onclick = () => vpPlay(v.reference_id);
      const del = el("button","mini","移除");
      del.onclick = () => vpRemove(v.reference_id);
      actions.appendChild(play); actions.appendChild(del);
      row.appendChild(actions);
      poolCard.appendChild(row);
    });
  }
  view.appendChild(poolCard);

  // ---- Fish 全部音色 ----
  const modelsCard = el("div","card");
  const mh = el("div","head");
  mh.appendChild(el("span","zh","Fish 账号全部音色"));
  mh.appendChild(el("span","grow"));
  const load = el("button","mini","加载 / 刷新");
  load.onclick = () => vpLoadModels();
  mh.appendChild(load);
  modelsCard.appendChild(mh);
  const search = vpInput("按名称 / id / 语言过滤");
  search.id = "vpSearch";
  search.style.marginBottom = "8px";
  search.oninput = () => renderVoicePoolModels();
  modelsCard.appendChild(search);
  const models = el("div");
  models.id = "vpModels";
  modelsCard.appendChild(models);
  view.appendChild(modelsCard);

  renderVoicePoolModels();
}


// ---------- 角色工坊 ----------
const PS_LANGS = ["en","zh","ja","ko","de","es","it","pt","fr"];
const PS_GENDERS = [["female","女"],["male","男"],["","未知"]];
const PS_STATUS = [["active","active"],["paused","paused"],["inactive","inactive"]];
const PS_LIFECYCLE = [["permanent","permanent"],["guest","guest"]];

function psMsg(kind, text){
  const box = $("psMsg");
  if(box){
    box.hidden = false;
    box.className = "vpmsg " + kind;
    box.textContent = text;
    return;
  }
  // 图片库等没有 #psMsg 的页面：退回顶部全局消息条，绝不弹 alert 阻塞页面
  try{ show(kind === "bad" ? "bad" : (kind === "warn" ? "warn" : "ok"), text); }catch(e){}
}

function psField(label, key, opts){
  opts = opts || {};
  const row = el("div","kvrow");
  const left = el("div","kvleft");
  left.appendChild(el("div","klabel", label));
  if(opts.hint) left.appendChild(el("div","use", opts.hint));
  row.appendChild(left);
  let node;
  if(opts.options){
    node = vpSel(opts.options);
  } else if(opts.textarea){
    node = document.createElement("textarea");
    node.className = "cfginput";
    node.rows = opts.rows || 3;
  } else {
    node = vpInput(opts.placeholder || "");
    node.type = opts.type || "text";
  }
  node.id = "psF_" + key;
  if(opts.value !== undefined && opts.value !== null) node.value = String(opts.value);
  row.appendChild(node);
  return row;
}

function psEditorData(){
  const val = (key) => { const n = $("psF_" + key); return n ? n.value : ""; };
  const data = {
    nickname: (val("nickname") || "").trim(),
    gender: val("gender"),
    age: parseInt(val("age") || "25", 10) || 25,
    language: val("language"),
    country: (val("country") || "").trim(),
    city: (val("city") || "").trim(),
    occupation: (val("occupation") || "").trim(),
    bio: val("bio"),
    personality: val("personality"),
    interests: (val("interests") || "").split(/[,，]/).map(s => s.trim()).filter(Boolean),
    fish_voice_id: (val("fish_voice_id") || "").trim(),
    voice_id: (val("voice_id") || "").trim(),
    archetype_id: (val("archetype_id") || "").trim(),
    status: val("status"),
    lifecycle: val("lifecycle"),
    expires_at: (val("expires_at") || "").trim(),
    avatar_seed: (val("avatar_seed") || "").trim(),
    avatar_style: (val("avatar_style") || "").trim(),
  };
  for(const key of ["speech_dna","ai_config"]){
    const raw = (val(key) || "").trim();
    if(!raw){ continue; }
    try { data[key] = JSON.parse(raw); } catch(e){ throw new Error(key + " 不是合法 JSON：" + e.message); }
  }
  return data;
}

async function openPersonaStudio(){
  state.name = "personas";
  hideMsg();
  history.replaceState(null, "", "#personas");
  try{
    const data = await api("GET","/api/admin/personas");
    if(!data.ok){ show("bad","读取角色失败：" + (data.issues || ["未知"]).join("；")); return; }
    state.personaList = data.personas || [];
    state.personaCounts = data.counts || {};
    state.personaUsers = data.users || [];
    state.personaOk = state.personaList.length > 0;
    try{
      const vpData = await api("GET","/api/admin/voice-pool");
      state.poolVoices = vpData.voices || [];
    }catch(e){ state.poolVoices = []; }
    state.editing = null;
    state.creating = false;
    render();
  }catch(e){ show("bad","读取角色失败：" + e.message); }
}

async function psOpenEdit(id){
  try{
    const data = await api("GET","/api/admin/personas/" + encodeURIComponent(id));
    if(!data.ok){ show("bad","读取角色失败"); return; }
    state.editing = data.persona;
    state.creating = false;
    renderPersonaStudioView();
  }catch(e){ show("bad","读取角色失败：" + e.message); }
}

function psOpenCreate(){
  state.editing = {
    id: "", nickname: "", gender: "female", age: 25, language: "en",
    country: "", city: "", occupation: "", bio: "", personality: "",
    interests: [], fish_voice_id: "", voice_id: "", archetype_id: "",
    status: "active", lifecycle: "permanent", expires_at: "",
    avatar_seed: "persona-preview", avatar_style: "avataaars",
    speech_dna: {}, ai_config: {source: "admin-test", posts: []},
  };
  state.creating = true;
  renderPersonaStudioView();
}

async function psSave(){
  let data;
  try { data = psEditorData(); } catch(e){ psMsg("bad", e.message); return; }
  if(!data.nickname){ psMsg("bad","昵称不能为空"); return; }
  try{
    if(state.creating){
      const bindSel = $("psBindUser");
      const postsRaw = ($("psPosts") || {}).value || "";
      data.bind_user_id = bindSel ? bindSel.value : "";
      data.posts = postsRaw.split("\\n").map(s => s.trim()).filter(Boolean).slice(0,5);
      const res = await fetch("/api/admin/personas", {
        method: "POST", headers: vpHeaders(), body: JSON.stringify(data),
      }).then(r => r.json());
      if(!res.ok){ psMsg("bad","新建失败：" + (res.issues || ["未知"]).join("；")); return; }
      psMsg("ok", "已新建角色：" + (res.persona || {}).nickname);
      await openPersonaStudio();
    } else {
      const res = await fetch("/api/admin/personas/" + encodeURIComponent(state.editing.id), {
        method: "PUT", headers: vpHeaders(), body: JSON.stringify(data),
      }).then(r => r.json());
      if(!res.ok){ psMsg("bad","保存失败：" + (res.issues || ["未知"]).join("；")); return; }
      state.editing = res.persona;
      psMsg("ok", "已保存（同步好友行 " + (res.friends_synced || 0) + " 条）");
      renderPersonaStudioView();
    }
  }catch(e){ psMsg("bad","保存失败：" + e.message); }
}

async function psReroll(id){
  if(!confirm("重捏这个角色？昵称/年龄/城市/职业/兴趣/性别/头像/音色会重新随机（角色 ID、好友、聊天保留）。")) return;
  try{
    const res = await fetch("/api/admin/personas/" + encodeURIComponent(id) + "/reroll", {
      method: "POST", headers: vpHeaders(), body: JSON.stringify({ regen_voice: true, regen_avatar: true }),
    }).then(r => r.json());
    if(!res.ok){ psMsg("bad","重捏失败"); return; }
    const p = res.persona || {};
    psMsg("ok", "已重捏：" + (res.before ? res.before.nickname + " → " : "") + p.nickname + "（" + (p.city || "") + "）");
    state.editing = res.persona;
    state.personaList = (await api("GET","/api/admin/personas")).personas;
    renderPersonaStudioView();
  }catch(e){ psMsg("bad","重捏失败：" + e.message); }
}

async function psDelete(id){
  if(!confirm("删除这个 AI 角色？会删除它的动态/示例对话/关系/好友关系和相关聊天，并先备份数据库。")) return;
  try{
    const res = await fetch("/api/admin/personas/" + encodeURIComponent(id), {
      method: "DELETE", headers: vpHeaders(),
    }).then(r => r.json());
    if(!res.ok){ psMsg("bad","删除失败"); return; }
    psMsg("ok", "已删除。数据库备份：" + (res.backup || "(无)"));
    await openPersonaStudio();
  }catch(e){ psMsg("bad","删除失败：" + e.message); }
}

async function psPreviewVoice(){
  const input = $("psF_fish_voice_id");
  const rid = (input.value || "").trim();
  if(!rid){ psMsg("warn","先填 Fish 音色名称或 id"); return; }
  psMsg("info","正在合成试听…");
  try{
    const audio = $("psPlayer");
    const blob = await fetch("/api/admin/voice-pool/preview", {
      method: "POST", headers: vpHeaders(), body: JSON.stringify({ reference_id: rid }),
    }).then(r => { if(!r.ok) throw new Error("HTTP " + r.status); return r.blob(); });
    audio.src = URL.createObjectURL(blob);
    audio.hidden = false;
    audio.play();
    psMsg("ok","正在播放：" + rid);
  }catch(e){ psMsg("bad","试听失败：" + e.message); }
}

async function psFixMedia(){
  if(!confirm("用 LLM 按图片内容重写图文动态的文案（每点一次修 20 条，图片不变，只重写文字）。继续？")) return;
  try{
    const res = await fetch("/api/admin/personas/rematch-media", {
      method: "POST", headers: vpHeaders(), body: JSON.stringify({ limit: 20 }),
    }).then(r => r.json());
    if(!res.ok){ psMsg("bad","修正失败"); return; }
    psMsg("ok", "已修正 " + res.fixed + " 条（失败 " + res.failed + "），剩余 " + res.remaining + " 条；可再点一次继续");
  }catch(e){ psMsg("bad","修正失败：" + e.message); }
}

async function psFixGender(){
  if(!confirm("把现有 AI 角色性别调整到约 1:1？（会更换部分角色的名字/头像/声音；角色 ID、好友、聊天保留，先备份数据库）")) return;
  try{
    const res = await fetch("/api/admin/personas/rebalance-genders", {
      method: "POST", headers: vpHeaders(), body: JSON.stringify({ female_ratio: 0.5 }),
    }).then(r => r.json());
    if(!res.ok){ psMsg("bad","调整失败：" + (res.issues || ["未知"]).join("；")); return; }
    psMsg("ok", "性别已平衡：女 " + res.female + " / 男 " + res.male + "（调整 " + res.changed + " 人，音色已按性别重配）");
    await openPersonaStudio();
  }catch(e){ psMsg("bad","调整失败：" + e.message); }
}

async function psFixVoices(){
  if(!confirm("把素材池里的音色重新均匀分配给全部角色？（同一角色 ID/好友/聊天不变，只换声音；先备份数据库）")) return;
  try{
    const res = await fetch("/api/admin/personas/redistribute-voices", {
      method: "POST", headers: vpHeaders(),
    }).then(r => r.json());
    if(!res.ok){ psMsg("bad","分配失败：" + (res.issues || ["未知"]).join("；")); return; }
    const dist = (res.distribution || []).map(x => x.name + "×" + x.count).join("，");
    psMsg("ok", "已重新分配 " + res.updated + " 个角色 · " + dist);
    await openPersonaStudio();
  }catch(e){ psMsg("bad","分配失败：" + e.message); }
}

async function psFixPosts(){
  const nText = prompt("给每个角色重新生成几条动态？（默认 5，现有动态会被替换）", "5");
  if(nText === null) return;
  const n = parseInt(nText || "5", 10) || 5;
  if(!confirm("将删除现有全部角色动态并重新生成，每人 " + n + " 条（错峰时间 + 按语言素材库随机）。继续？")) return;
  try{
    const res = await fetch("/api/admin/personas/regenerate-posts", {
      method: "POST", headers: vpHeaders(), body: JSON.stringify({ per_persona: n }),
    }).then(r => r.json());
    if(!res.ok){ psMsg("bad","生成失败"); return; }
    psMsg("ok", "已为 " + res.personas + " 个角色生成 " + res.posts + " 条动态");
    await openPersonaStudio();
  }catch(e){ psMsg("bad","生成失败：" + e.message); }
}

async function psFixLife(){
  if(!confirm("给全部角色补齐作息表 / Speech DNA + 示例对话 / 人物关系？（0 次 LLM，之后生活事件和事件动态会自动生成）")) return;
  try{
    const res = await fetch("/api/admin/personas/init-life", {
      method: "POST", headers: vpHeaders(),
    }).then(r => r.json());
    if(!res.ok){ psMsg("bad","补齐失败"); return; }
    psMsg("ok", "生活层补齐：角色 " + res.personas + " · 作息 " + res.schedules + " · 关系 " + res.relationships + " · DNA " + JSON.stringify(res.dna));
  }catch(e){ psMsg("bad","补齐失败：" + e.message); }
}

async function psRebuild(mode){
  try{
    const preview = await api("GET","/api/admin/personas/rebuild-preview");
    const counts = preview.counts || {};
    const summary = "人物 " + counts.personas + " / 动态 " + counts.persona_posts + " / 示例 " + counts.persona_example_dialogs + " / 好友关系 " + counts.friendships + " / 用户 " + counts.v3_users;
    if(mode === "reroll"){
      const okReroll = confirm("原地重捏全部角色？影响：" + summary + "。保留角色 ID、好友、聊天，只重掷人设/头像/音色。会先备份数据库。");
      if(!okReroll) return;
      const res = await fetch("/api/admin/personas/rebuild", {
        method: "POST", headers: vpHeaders(),
        body: JSON.stringify({ mode: "reroll" }),
      }).then(r => r.json());
      if(!res.ok){ psMsg("bad","重捏失败：" + (res.issues || ["未知"]).join("；")); return; }
      psMsg("ok", "已原地重捏 " + res.rerolled + " 个角色；备份：" + res.backup);
    } else {
      const countText = prompt("彻底重建：将删除全部 AI 角色 + 动态 + 示例 + 关系 + 好友关系 + 相关聊天（用户账号保留，先自动备份数据库）。重建角色数量（默认 " + counts.personas + "）：", String(counts.personas || 11));
      if(countText === null) return;
      const confirmText = prompt("输入 RESET 确认彻底重建：");
      if(confirmText === null) return;
      const autoFriend = confirm("重建后是否把所有新角色自动加为所有用户的好友？（确定=自动加，取消=只重建）");
      const res = await fetch("/api/admin/personas/rebuild", {
        method: "POST", headers: vpHeaders(),
        body: JSON.stringify({ mode: "recreate", count: parseInt(countText || "0", 10) || 0, confirm: confirmText, auto_friend_users: autoFriend }),
      }).then(r => r.json());
      if(!res.ok){ psMsg("bad","重建失败：" + (res.issues || ["未知"]).join("；")); return; }
      psMsg("ok", "已重建 " + res.created + " 个角色；删除 " + JSON.stringify(res.deleted) + "；备份：" + res.backup);
    }
    await openPersonaStudio();
  }catch(e){ psMsg("bad","重建失败：" + e.message); }
}

function renderPersonaStudioView(){
  const list = state.personaList || [];
  const counts = state.personaCounts || {};
  const view = $("view");
  view.innerHTML = "";

  const bar = el("div","bar");
  const t = el("div");
  t.appendChild(el("h2",null,"角色工坊"));
  t.appendChild(el("p",null,"全量重捏 / 彻底重建，或指定一个角色修改它的全部属性（昵称、性别、年龄、语言、城市、职业、兴趣、人设、Speech DNA、音色…）。改动会同步到聊天/朗读/通话。"));
  bar.appendChild(t); bar.appendChild(el("div","grow"));
  const stat = el("span","mini", "角色 " + (counts.personas || list.length) + " · 动态 " + (counts.persona_posts || 0) + " · 用户 " + (counts.v3_users || 0));
  bar.appendChild(stat);
  const refresh = el("button",null,"刷新");
  refresh.onclick = () => openPersonaStudio();
  bar.appendChild(refresh);
  view.appendChild(bar);

  const msg = el("div","vpmsg"); msg.id = "psMsg"; msg.hidden = true; view.appendChild(msg);
  const player = document.createElement("audio"); player.id = "psPlayer"; player.controls = true; player.hidden = true;
  player.style.width = "100%"; view.appendChild(player);

  const actions = el("div","addrow");
  const rerollAll = el("button","mini primary","全量原地重捏（保留ID/好友/聊天）");
  rerollAll.onclick = () => psRebuild("reroll");
  const recreate = el("button","mini","彻底重建（删角色+动态+聊天后重建）");
  recreate.onclick = () => psRebuild("recreate");
  const create = el("button","mini","新建测试角色");
  create.onclick = () => psOpenCreate();
  const fixMedia = el("button","mini","修正图文匹配");
  fixMedia.onclick = () => psFixMedia();
  const fixGender = el("button","mini","平衡男女比例");
  fixGender.onclick = () => psFixGender();
  const fixVoices = el("button","mini","重新分配音色");
  fixVoices.onclick = () => psFixVoices();
  const fixPosts = el("button","mini","重新生成动态");
  fixPosts.onclick = () => psFixPosts();
  const fixLife = el("button","mini","补齐生活层");
  fixLife.onclick = () => psFixLife();
  actions.appendChild(rerollAll); actions.appendChild(recreate); actions.appendChild(create);
  actions.appendChild(fixMedia); actions.appendChild(fixGender); actions.appendChild(fixVoices); actions.appendChild(fixPosts); actions.appendChild(fixLife);
  view.appendChild(actions);

  // 列表
  const listCard = el("div","card");
  const lh = el("div","head"); lh.appendChild(el("span","zh","全部角色（" + list.length + "）")); lh.appendChild(el("span","grow"));
  listCard.appendChild(lh);
  list.slice(0, 300).forEach((p) => {
    const row = el("div","vprow");
    const info = el("div","vpinfo");
    info.appendChild(el("div","vpname", p.nickname + "  ·  " + (p.gender === "female" ? "女" : p.gender === "male" ? "男" : "?") + " " + (p.age || "") + " 岁"));
    info.appendChild(el("div","vpmeta", [p.language, p.country, p.city, p.occupation].filter(Boolean).join(" · ") + " ｜ 音色 " + (p.fish_voice_name ? p.fish_voice_name + " (" + (p.fish_voice || "").slice(0, 8) + ")" : ((p.fish_voice || "").slice(0, 14) || "-")) + " ｜ 好友 " + (p.friend_count || 0) + " ｜ 动态 " + (p.post_count || 0) + " ｜ 示例 " + (p.example_count || 0)));
    row.appendChild(info);
    const acts = el("div","vpactions");
    const edit = el("button","mini","编辑");
    edit.onclick = () => psOpenEdit(p.id);
    acts.appendChild(edit);
    row.appendChild(acts);
    listCard.appendChild(row);
  });
  if(!list.length) listCard.appendChild(el("p","muted","暂无角色"));
  view.appendChild(listCard);

  // 编辑器
  const e = state.editing;
  if(e){
    const card = el("div","card");
    const h = el("div","head");
    h.appendChild(el("span","zh", (state.creating ? "新建测试角色" : "编辑角色：" + (e.nickname || e.id))));
    h.appendChild(el("span","grow"));
    card.appendChild(h);
    const grid = el("div");
    grid.appendChild(psField("昵称","nickname",{value:e.nickname}));
    grid.appendChild(psField("性别","gender",{options:PS_GENDERS, value:e.gender || "female"}));
    grid.appendChild(psField("年龄","age",{type:"number", value:e.age || 25}));
    grid.appendChild(psField("语言","language",{options:PS_LANGS.map(l => [l,l]), value:e.language || "en"}));
    grid.appendChild(psField("国家","country",{value:e.country}));
    grid.appendChild(psField("城市","city",{value:e.city}));
    grid.appendChild(psField("职业","occupation",{value:e.occupation}));
    grid.appendChild(psField("兴趣（逗号分隔）","interests",{value:(e.interests || []).join(", ")}));
    grid.appendChild(psField("简介 bio","bio",{textarea:true, rows:3, value:e.bio}));
    grid.appendChild(psField("性格 personality","personality",{textarea:true, rows:2, value:e.personality}));
    const voiceRow = el("div","kvrow");
    const vLeft = el("div","kvleft");
    vLeft.appendChild(el("div","klabel","Fish 音色（名称或 id）"));
    vLeft.appendChild(el("div","use", e.fish_voice_name ? ("当前：" + e.fish_voice_name) : "可填素材池里的名称，或 reference_id"));
    voiceRow.appendChild(vLeft);
    const vRight = el("div");
    const vSel = vpSel([["","从素材池选择…"]].concat((state.poolVoices || []).map(v => [v.reference_id, (v.name || v.reference_id) + " · " + String(v.reference_id).slice(0,8)])));
    vSel.id = "psF_voice_select";
    vSel.onchange = () => {
      const idInput = $("psF_fish_voice_id");
      if(idInput && vSel.value) idInput.value = vSel.value;
    };
    vRight.appendChild(vSel);
    const vInput = vpInput("名称或 reference_id");
    vInput.id = "psF_fish_voice_id";
    vInput.value = e.fish_voice_id || "";
    vRight.appendChild(vInput);
    voiceRow.appendChild(vRight);
    grid.appendChild(voiceRow);
    grid.appendChild(psField("Edge 兜底音色","voice_id",{value:e.voice_id, hint:"留空自动按语言选"}));
    grid.appendChild(psField("原型 archetype_id","archetype_id",{value:e.archetype_id}));
    grid.appendChild(psField("状态","status",{options:PS_STATUS, value:e.status || "active"}));
    grid.appendChild(psField("生命周期","lifecycle",{options:PS_LIFECYCLE, value:e.lifecycle || "permanent"}));
    grid.appendChild(psField("过期时间","expires_at",{value:e.expires_at, hint:"guest 角色用；permanent 留空"}));
    grid.appendChild(psField("头像 seed","avatar_seed",{value:e.avatar_seed}));
    grid.appendChild(psField("头像风格","avatar_style",{value:e.avatar_style}));
    grid.appendChild(psField("Speech DNA（JSON）","speech_dna",{textarea:true, rows:4, value:JSON.stringify(e.speech_dna || {}, null, 1)}));
    grid.appendChild(psField("ai_config（JSON）","ai_config",{textarea:true, rows:3, value:JSON.stringify(e.ai_config || {}, null, 1)}));
    if(state.creating){
      const bindRow = el("div","kvrow");
      const left = el("div","kvleft"); left.appendChild(el("div","klabel","绑定用户（可选）"));
      left.appendChild(el("div","use","选一个用户，角色会直接出现在 TA 的消息列表里"));
      const sel = vpSel([["","不绑定"]].concat((state.personaUsers || []).map(u => [u.id, (u.display_name || u.id.slice(0,10)) + " · 好友" + (u.friend_count || 0)])));
      sel.id = "psBindUser";
      bindRow.appendChild(left); bindRow.appendChild(sel); grid.appendChild(bindRow);

      const postRow = el("div","kvrow");
      const l2 = el("div","kvleft"); l2.appendChild(el("div","klabel","初始动态（可选）"));
      l2.appendChild(el("div","use","每行一条，最多 5 条"));
      const ta = document.createElement("textarea"); ta.className = "cfginput"; ta.rows = 3; ta.id = "psPosts";
      postRow.appendChild(l2); postRow.appendChild(ta); grid.appendChild(postRow);
    }
    card.appendChild(grid);

    const btns = el("div","addrow");
    const save = el("button","mini primary","保存");
    save.onclick = () => psSave();
    btns.appendChild(save);
    if(!state.creating){
      const rr = el("button","mini","重捏此人");
      rr.onclick = () => psReroll(e.id);
      btns.appendChild(rr);
      const del = el("button","mini","删除此人");
      del.onclick = () => psDelete(e.id);
      btns.appendChild(del);
    } else {
      const cancel = el("button","mini","取消");
      cancel.onclick = () => { state.editing = null; state.creating = false; renderPersonaStudioView(); };
      btns.appendChild(cancel);
    }
    const pv = el("button","mini","试听音色");
    pv.onclick = () => psPreviewVoice();
    btns.appendChild(pv);
    card.appendChild(btns);
    view.appendChild(card);
  }
}

// ---------- APK 发布 ----------
function bytesText(n){
  const num = Number(n || 0);
  if(num >= 1024 * 1024) return (num / 1024 / 1024).toFixed(1) + " MB";
  if(num >= 1024) return (num / 1024).toFixed(1) + " KB";
  return num + " B";
}

function renderReleaseView(){
  const data = state.release || {};
  const cur = data.current || { available: false };
  const view = $("view");
  view.innerHTML = "";
  const bar = el("div","bar");
  const t = el("div");
  t.appendChild(el("h2",null,"APK 发布 / 在线更新"));
  t.appendChild(el("p",null,"上传新的安装包后，App 启动时会请求 /api/app/latest：版本号更高就提示用户下载安装。版本号必须递增、必须用同一套签名打包；旧版本自动进历史，可一键回滚。"));
  bar.appendChild(t); bar.appendChild(el("div","grow"));
  const refresh = el("button",null,"刷新");
  refresh.onclick = () => openRelease();
  bar.appendChild(refresh);
  view.appendChild(bar);

  const curCard = el("div","card");
  const head = el("div","head");
  head.appendChild(el("span","zh","当前线上版本"));
  head.appendChild(el("span","grow"));
  curCard.appendChild(head);
  if(cur.available){
    const rows = [
      ["版本", cur.versionName + "（versionCode " + cur.versionCode + "）"],
      ["大小", bytesText(cur.size)],
      ["SHA-256", cur.sha256],
      ["更新时间", cur.publishedAt || "-"],
      ["更新说明", cur.changelog || "（空）"],
      ["强制更新", cur.force ? "是（低于 minVersionCode " + cur.minVersionCode + " 时不更新无法使用）" : (cur.minVersionCode ? "低于 " + cur.minVersionCode + " 时强制" : "否（用户可跳过）")],
    ];
    rows.forEach(([k,v]) => {
      const row = el("div","kvrow");
      const left = el("div","kvleft"); left.appendChild(el("div","klabel", k));
      row.appendChild(left);
      const val = el("div"); val.textContent = String(v); val.style.flex = "1"; val.style.fontSize = "12.5px";
      if(k === "SHA-256") val.style.wordBreak = "break-all";
      row.appendChild(val);
      curCard.appendChild(row);
    });
    const dl = el("a","mini", "下载当前 APK");
    dl.href = cur.apkUrl; dl.target = "_blank"; dl.rel = "noreferrer";
    dl.style.display = "inline-block";
    curCard.appendChild(dl);
  }else{
    curCard.appendChild(el("p","muted","还没有发布过 APK。下面上传一个即可。"));
  }
  view.appendChild(curCard);

  // 上传表单
  const form = el("div","card");
  const fh = el("div","head"); fh.appendChild(el("span","zh","发布新版本")); fh.appendChild(el("span","grow"));
  form.appendChild(fh);
  const mk = (label, node, hint) => {
    const row = el("div","kvrow");
    const left = el("div","kvleft");
    left.appendChild(el("div","klabel", label));
    if(hint) left.appendChild(el("div","use", hint));
    row.appendChild(left); row.appendChild(node); form.appendChild(row);
  };
  const file = document.createElement("input"); file.type = "file"; file.id = "relFile"; file.accept = ".apk";
  file.className = "cfginput";
  mk("APK 文件", file, "用同一套 keystore 签名、versionCode 递增");
  const code = document.createElement("input"); code.id = "relCode"; code.className = "cfginput";
  code.placeholder = "例如 301"; code.type = "number"; code.min = "1";
  mk("versionCode", code, "必须比线上版本大（Android 用来判断是否升级）");
  const name = document.createElement("input"); name.id = "relName"; name.className = "cfginput";
  name.placeholder = "例如 3.0.1";
  mk("versionName", name, "给用户看的版本号");
  const minv = document.createElement("input"); minv.id = "relMin"; minv.className = "cfginput";
  minv.placeholder = "例如 300（低于它的旧版强制升级）"; minv.type = "number"; minv.min = "0";
  mk("minVersionCode", minv, "可选：低于这个版本必须更新才能继续用");
  const forceRow = el("div","kvrow");
  const fl = el("div","kvleft"); fl.appendChild(el("div","klabel","强制更新"));
  forceRow.appendChild(fl);
  const force = document.createElement("input"); force.type = "checkbox"; force.id = "relForce";
  const flWrap = el("div"); flWrap.appendChild(force); flWrap.appendChild(el("span","use"," 忽略 minVersionCode，所有旧版都必须更新"));
  forceRow.appendChild(flWrap);
  form.appendChild(forceRow);
  const ta = document.createElement("textarea"); ta.id = "relChangelog"; ta.className = "cfginput";
  ta.rows = 3; ta.placeholder = "更新说明（App 弹窗里展示）";
  mk("更新说明", ta);
  const bar2 = el("div","addrow");
  const upload = el("button","mini primary","上传并发布");
  upload.onclick = () => uploadRelease();
  const rollback = el("button","mini","回滚上一版");
  rollback.onclick = () => rollbackRelease();
  bar2.appendChild(upload); bar2.appendChild(rollback);
  form.appendChild(bar2);
  view.appendChild(form);

  // 历史
  if((data.history || []).length){
    const hist = el("div","card");
    const hh = el("div","head"); hh.appendChild(el("span","zh","历史版本（可回滚）")); hh.appendChild(el("span","grow"));
    hist.appendChild(hh);
    data.history.forEach((item) => {
      const row = el("div","kvrow");
      const left = el("div","kvleft");
      left.appendChild(el("div","klabel", item.versionName + "（" + item.versionCode + "）"));
      left.appendChild(el("div","use", (item.publishedAt || "") + " · " + bytesText(item.size)));
      row.appendChild(left);
      const right = el("div"); right.style.fontSize = "12px"; right.style.color = "var(--muted)";
      right.textContent = item.changelog || "";
      row.appendChild(right);
      hist.appendChild(row);
    });
    view.appendChild(hist);
  }
}

// ---------- JSON 只读预览 ----------
function jsonPreview(lib){
  const d = document.createElement("details");
  d.className = "json";
  const s = document.createElement("summary");
  s.textContent = "高级：查看当前 JSON（只读，用于排查）";
  d.appendChild(s);
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(lib.data, null, 2);
  d.appendChild(pre);
  return d;
}


// ---------- 图片库 ----------
async function openMediaLibrary(){
  state.name = "media";
  try{
    state.mediaStats = await api("GET","/api/admin/media/stats");
  }catch(e){
    state.mediaStats = null;
  }
  if(!state.mediaResults) state.mediaResults = [];
  try{
    await mediaLoadLibrary(true);
  }catch(e){}
  render(); renderNav();
}

async function mediaLoadLibrary(reset){
  if(reset) state.mediaLibraryItems = [];
  const offset = (state.mediaLibraryItems || []).length;
  const res = await api("GET","/api/admin/media/library?offset=" + offset + "&limit=60");
  state.mediaLibraryTotal = res.total || 0;
  state.mediaLibraryItems = (state.mediaLibraryItems || []).concat(res.items || []);
  return res;
}

async function mediaLoadMore(){
  try{
    await mediaLoadLibrary(false);
    renderMediaView();
  }catch(e){ psMsg("bad","加载失败：" + e.message); }
}

function mediaStatsBox(){
  const s = state.mediaStats;
  const box = el("div","card");
  if(!s){ box.appendChild(el("div","muted","图库状态读取失败")); return box; }
  box.appendChild(el("div","muted","图片库：" + s.files + " 张 · " + s.bytes_mb + " MB（被动态引用 " + s.referenced_files + " 张 / " + s.referenced_bytes_mb + " MB）"));
  const lim = s.limits || {};
  box.appendChild(el("div","muted","上限：" + (lim.max_files||"-") + " 张 / " + Math.round((lim.max_bytes||0)/1048576) + " MB · TTL " + (lim.ttl_days||"-") + " 天 · 后台每小时自动清理"));
  return box;
}

async function mediaSearch(){
  const input = $("mediaQuery");
  const query = ((input && input.value) || "").trim();
  if(!query){ psMsg("bad","先输入搜索词"); return; }
  try{
    const res = await api("GET","/api/admin/media/search?query=" + encodeURIComponent(query) + "&limit=12");
    state.mediaResults = Array.isArray(res.items) ? res.items : [];
    state.mediaQuery = query;
    renderMediaView();
    psMsg(state.mediaResults.length ? "ok" : "warn", "搜索「" + query + "」：找到 " + state.mediaResults.length + " 张");
  }catch(e){ psMsg("bad","搜索失败：" + e.message); }
}

async function mediaDownload(idx){
  const item = (state.mediaResults||[])[idx];
  if(!item) return;
  try{
    const res = await api("POST","/api/admin/media/fetch", {query: state.mediaQuery || item.title, external_id: item.external_id, limit: 1});
    if(!res.ok){ psMsg("bad",(res.issues||["下载失败"]).join("；")); return; }
    item.downloaded = true;
    if(res.stats) state.mediaStats = res.stats;
    renderMediaView();
    psMsg("ok","已入库：" + ((res.saved && res.saved[0] && res.saved[0].title) || item.title));
  }catch(e){ psMsg("bad","下载失败：" + e.message); }
}

async function mediaFill(){
  const nText = prompt("每个角色目标有几条带图动态？（默认 2，最多 3）","2");
  if(nText === null) return;
  const per = parseInt(nText||"1",10)||1;
  if(!confirm("按角色兴趣搜图：每批最多 8 个角色，每角色 " + per + " 张，并自动配一条配文动态。继续？")) return;
  let totalAdded=0, totalPosts=0, batches=0;
  while(batches < 20){
    try{
      const res = await api("POST","/api/admin/media/fill",{per_persona:per, with_caption:true});
      if(!res.ok){ psMsg("bad",(res.issues||["补图失败"]).join("；")); break; }
      totalAdded += res.images_added||0; totalPosts += res.posts_created||0;
      if(res.stats) state.mediaStats = res.stats;
      psMsg("ok","补图中… 新增图 " + totalAdded + " 张 / 配文 " + totalPosts + " 条，剩余角色 " + (res.remaining_personas||0));
      if(!res.remaining_personas || (res.images_added===0 && res.posts_created===0)) break;
      batches++;
      await new Promise(r => setTimeout(r, 300));
    }catch(e){ psMsg("bad","补图失败：" + e.message); break; }
  }
  renderMediaView();
  psMsg("ok","补图完成：新增图片 " + totalAdded + " 张，配文动态 " + totalPosts + " 条");
}

async function mediaPrune(){
  if(!confirm("清理过期（未被引用的）和超配额的图片？被动态引用的图片不会删除。")) return;
  try{
    const res = await api("POST","/api/admin/media/prune",{});
    state.mediaStats = await api("GET","/api/admin/media/stats");
    renderMediaView();
    psMsg("ok","清理完成：删除 " + res.deleted_files + " 张 / " + Math.round((res.deleted_bytes||0)/1024) + " KB，剩余 " + res.remaining_files + " 张");
  }catch(e){ psMsg("bad","清理失败：" + e.message); }
}

function renderMediaView(){
  const view = $("view"); view.innerHTML = "";
  const bar = el("div","bar");
  const t = el("div");
  t.appendChild(el("h2",null,"图片库"));
  t.appendChild(el("p",null,"按兴趣搜图 / 下载入库 / 给角色补配文动态。库有 TTL 和总上限，后台每小时自动清理；被动态引用的图永不删除。"));
  bar.appendChild(t); bar.appendChild(el("div","grow"));
  const fill = el("button","mini","按角色兴趣补图");
  fill.onclick = () => mediaFill();
  const prune = el("button","mini","清理图库");
  prune.onclick = () => mediaPrune();
  const refresh = el("button","mini","刷新");
  refresh.onclick = () => openMediaLibrary();
  bar.appendChild(fill); bar.appendChild(prune); bar.appendChild(refresh);
  view.appendChild(bar);
  view.appendChild(mediaStatsBox());

  const searchCard = el("div","card");
  const row = el("div");
  row.style.display = "flex"; row.style.gap = "8px"; row.style.marginTop = "6px";
  const input = document.createElement("input");
  input.id = "mediaQuery";
  input.placeholder = "搜索图片（例如 coffee, bicycle, books, espresso…）";
  input.value = state.mediaQuery || "";
  input.style.flex = "1";
  input.addEventListener("keydown", (ev) => { if(ev.key === "Enter") mediaSearch(); });
  const btn = el("button","mini","搜图");
  btn.onclick = () => mediaSearch();
  row.appendChild(input); row.appendChild(btn);
  searchCard.appendChild(row);
  view.appendChild(searchCard);

  const results = state.mediaResults || [];
  if(results.length){
    view.appendChild(el("div","muted","搜索结果：" + results.length + " 张 —— 点「下载入库」加入图库，然后去「按角色兴趣补图」让它自动配文发动态。"));
    const grid = el("div","grid");
    results.forEach((item, idx) => {
      const card = el("div","card");
      if(item.thumb_url){
        const img = document.createElement("img");
        img.src = item.thumb_url;
        img.loading = "lazy";
        img.style.width = "100%";
        img.style.borderRadius = "8px";
        img.style.marginBottom = "8px";
        card.appendChild(img);
      }
      card.appendChild(el("div",null,(item.title || "").slice(0,90)));
      card.appendChild(el("div","muted",(item.source || "") + " · " + (item.license || "") + (item.author ? " · " + item.author : "")));
      const dl = el("button","mini", item.downloaded ? "已入库" : "下载入库");
      dl.disabled = !!item.downloaded;
      dl.onclick = () => mediaDownload(idx);
      card.appendChild(dl);
      grid.appendChild(card);
    });
    view.appendChild(grid);
  }

  // 图库全部图片（默认展示）
  const libItems = state.mediaLibraryItems || [];
  const libHead = el("div","bar");
  const libTitle = el("div");
  libTitle.appendChild(el("h2",null,"图库全部图片（" + (state.mediaLibraryTotal || libItems.length) + " 张）"));
  libTitle.appendChild(el("p",null,"已入库的图片都会显示在这里；绿色「已用于动态」= 正在被信息流引用（不会被清理）。"));
  libHead.appendChild(libTitle);
  const more = el("button","mini","加载更多");
  more.onclick = () => mediaLoadMore();
  more.disabled = libItems.length >= (state.mediaLibraryTotal || 0);
  libHead.appendChild(more);
  view.appendChild(libHead);
  if(libItems.length){
    const libGrid = el("div","grid");
    libItems.forEach((item) => {
      const card = el("div","card");
      const img = document.createElement("img");
      img.src = item.url;
      img.loading = "lazy";
      img.style.width = "100%";
      img.style.borderRadius = "8px";
      img.style.marginBottom = "8px";
      card.appendChild(img);
      card.appendChild(el("div",null,(item.title || "(无标题)").slice(0,90)));
      card.appendChild(el("div","muted",(item.source || "") + " · " + (item.license || "") + (item.author ? " · " + item.author : "")));
      const badge = el("div","muted", item.referenced ? "✅ 已用于动态" : "○ 未使用" + (item.persona_nickname ? " · 来源角色 " + item.persona_nickname : ""));
      if(item.referenced) badge.style.color = "#5bd1a5";
      card.appendChild(badge);
      libGrid.appendChild(card);
    });
    view.appendChild(libGrid);
  }else{
    view.appendChild(el("div","muted","图库是空的：先搜图下载，或点右上角「按角色兴趣补图」。"));
  }
}

// ---------- 视图路由 ----------
const RENDERERS = { dna: renderDNA, scenes: renderScenes, phrases: renderPhrases, events: renderEvents, rhythm: renderRhythm, world: renderWorld };

function render(){
  if(state.name === "config"){ renderConfigView(); renderNav(); return; }
  if(state.name === "release"){ renderReleaseView(); renderNav(); return; }
  if(state.name === "voices"){ renderVoicePoolView(); renderNav(); return; }
  if(state.name === "personas"){ renderPersonaStudioView(); renderNav(); return; }
  if(state.name === "media"){ renderMediaView(); renderNav(); return; }
  const lib = state.libs[state.name];
  if(!lib) return;
  const view = $("view");
  view.innerHTML = "";

  // 顶部操作条
  const bar = el("div","bar");
  const t = el("div");
  t.appendChild(el("h2",null, NAV_ZH[state.name] || state.name));
  t.appendChild(el("p",null, DESC[state.name] || "改完点右侧「保存并生效」；校验不过不会写入。"));
  bar.appendChild(t); bar.appendChild(el("div","grow"));
  if(state.name === "world"){
    const sel = document.createElement("select");
    sel.className = "langsel";
    const pools = (lib.data && lib.data.names) || {};
    const langs = Object.keys(pools);
    if(langs.length === 0) langs.push("en");
    langs.forEach((code) => {
      const opt = document.createElement("option");
      opt.value = code;
      opt.textContent = (LANG_ZH[code] || code) + " · " + code;
      sel.appendChild(opt);
    });
    bar.appendChild(sel);
    const pv = el("button",null,"预览捏人");
    pv.title = "按当前素材池试捏 4 个人物（不写库、不调模型）";
    pv.onclick = () => previewPersonas(sel.value);
    bar.appendChild(pv);
  }
  if(state.name === "rhythm" || state.name === "events"){
    const sim = el("button",null,"模拟今天");
    sim.title = "按当前参数估算今天会产出多少事件/动态/评论（不写库、不调模型）";
    sim.onclick = () => simulateToday();
    bar.appendChild(sim);
  }
  const vbtn = el("button",null,"校验");
  vbtn.onclick = () => validate(lib);
  const rbtn = el("button","ghost","回滚上一版");
  rbtn.onclick = () => resetLib();
  const sbtn = el("button","primary","保存并生效");
  sbtn.onclick = () => saveLib(lib);
  bar.appendChild(vbtn); bar.appendChild(rbtn); bar.appendChild(sbtn);
  view.appendChild(bar);

  const body = (RENDERERS[state.name] || renderDNA)(lib);
  if (body) view.appendChild(body);
  renderNav();
}

// ---------- 数据操作 ----------
async function openConfig(){
  state.name = "config";
  hideMsg();
  history.replaceState(null, "", "#config");
  try{
    state.config = await api("GET","/api/admin/config");
  }catch(e){
    show("bad","读取配置失败：" + e.message);
    return;
  }
  render();
}

async function openRelease(){
  state.name = "release";
  hideMsg();
  history.replaceState(null, "", "#release");
  try{
    state.release = await api("GET","/api/admin/app-release");
    state.releaseOk = !!(state.release.current && state.release.current.available);
  }catch(e){
    show("bad","读取 APK 发布状态失败：" + e.message);
    return;
  }
  render();
}

async function uploadRelease(){
  const f = $("relFile");
  if(!f || !f.files || !f.files[0]){ show("warn","先选择一个 .apk 文件"); return; }
  const code = ($("relCode").value || "").trim();
  const name = ($("relName").value || "").trim();
  if(!/^\d+$/.test(code) || Number(code) <= 0){ show("bad","versionCode 必须是正整数（且比上一版大）"); return; }
  if(!name){ show("bad","versionName 不能为空，例如 3.0.1"); return; }
  const fd = new FormData();
  fd.append("file", f.files[0]);
  fd.append("version_code", code);
  fd.append("version_name", name);
  fd.append("changelog", ($("relChangelog").value || "").trim());
  fd.append("force", $("relForce").checked ? "true" : "false");
  fd.append("min_version_code", ($("relMin").value || "0").trim() || "0");
  try{
    const headers = {};
    const token = window.__SE_ADMIN_TOKEN__ || sessionStorage.getItem("seAdminToken") || "";
    if(token) headers["X-Admin-Token"] = token;
    const res = await fetch("/api/admin/app-release/upload", { method:"POST", headers, body: fd });
    const data = await res.json().catch(()=>({}));
    if(!res.ok) throw new Error(data.detail || ("HTTP " + res.status));
    if(!data.ok){ show("bad","发布被拒绝：\\n· " + (data.issues || ["未知"]).join("\\n· ")); return; }
    show("ok", "已发布 " + data.current.versionName + "（versionCode " + data.current.versionCode + "）✓");
    await openRelease();
  }catch(e){ show("bad","上传失败：" + e.message); }
}

async function rollbackRelease(){
  if(!confirm("回滚到上一版 APK？")) return;
  try{
    const res = await api("POST","/api/admin/app-release/rollback");
    if(!res.ok){ show("bad","回滚失败：" + (res.issues || ["未知"]).join("；")); return; }
    show("ok","已回滚到 " + res.current.versionName + "（versionCode " + res.current.versionCode + "）");
    await openRelease();
  }catch(e){ show("bad","回滚失败：" + e.message); }
}

async function openLib(name){
  if(name === "config"){ await openConfig(); return; }
  if(name === "release"){ await openRelease(); return; }
  if(name === "voices"){ await openVoicePool(); return; }
  if(name === "personas"){ await openPersonaStudio(); return; }
  state.name = name;
  if(location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
  hideMsg();
  if(!state.libs[name]){
    const res = await api("GET","/api/admin/lexicons/" + name);
    state.libs[name] = { data: res.data || {}, exists: res.exists, issues: res.issues || [] };
  }
  render();
  if(!state.libs[name].exists) show("warn","该词库文件还不存在；保存后会创建。");
}

function collect(){ return state.libs[state.name].data; }

async function validate(lib){
  try{
    const res = await api("POST", `/api/admin/lexicons/${state.name}/validate`, { data: collect() });
    if(res.ok) show("ok","校验通过 ✓");
    else show("bad","校验未通过：\\n· " + res.issues.join("\\n· "));
  }catch(e){ show("bad","校验失败：" + e.message); }
}

async function saveLib(lib){
  try{
    const res = await api("PUT", "/api/admin/lexicons/" + state.name, { data: collect() });
    if(res.ok){
      show("ok","已保存并生效 ✓" + (res.backup ? "（上一版已备份）" : ""));
      state.libs[state.name].exists = true;
      await refreshStatus();
      await refreshUsage();
    }else{
      show("bad","保存被拒绝（没有写入）：\\n· " + (res.issues||[]).join("\\n· "));
    }
  }catch(e){ show("bad","保存失败：" + e.message); }
}

async function resetLib(){
  if(!confirm("回滚到上一版（或代码默认值）？")) return;
  try{
    const res = await api("POST", `/api/admin/lexicons/${state.name}/reset`);
    delete state.libs[state.name];
    show("ok","已回滚：" + (res.restored||""));
    await refreshStatus();
    await openLib(state.name);
  }catch(e){ show("bad","回滚失败：" + e.message); }
}

async function refreshStatus(){
  const st = await api("GET","/api/admin/status");
  state.items = st.lexicons;
  const a = st.applied || {};
  $("applied").textContent = `已生效：DNA ${a.dna_fields||0} 字段 / 场景 ${a.scenes||0} / 禁用词 ${a.banned||0}`;
  renderNav();
}

async function refreshUsage(){
  try{
    state.usage = await api("GET","/api/admin/usage");
    renderNav();
    if(state.name) render();
  }catch(e){ /* 统计失败不影响编辑 */ }
}

(async () => {
  try{
    await refreshStatus();
    await refreshUsage();
    const want = (location.hash || "").replace("#", "");
    if(want === "config"){
      await openConfig();
    }else if(want === "release"){
      await openRelease();
    }else if(want === "voices"){
      await openVoicePool();
    }else if(want === "personas"){
      await openPersonaStudio();
    }else{
      const first = state.items.find(i=>i.name===want)
                 || state.items.find(i=>i.name==="dna") || state.items[0];
      if(first) await openLib(first.name);
    }
    // 支持 /admin?preview=en#world 直接看捏人预览（便于分享和截图）
    const autoPreview = new URLSearchParams(location.search).get("preview");
    if(autoPreview){
      state.name = "world";
      await openLib("world");
      await previewPersonas(autoPreview);
    }
  }catch(e){ show("bad","初始化失败：" + e.message); }
})();
</script>
</body>
</html>
"""


def render_admin_page(*, admin_token: str = "") -> str:
    """渲染管理台；远程访问时把口令注入这次响应，不写进静态文件。"""
    if not admin_token:
        return ADMIN_HTML
    snippet = (
        "window.__SE_ADMIN_TOKEN__ = " + json.dumps(admin_token) + ";"
        'sessionStorage.setItem("seAdminToken", window.__SE_ADMIN_TOKEN__);'
        'try{const u=new URL(location.href);u.searchParams.delete("token");'
        'history.replaceState(null,"",u.pathname+u.hash);}catch(e){}'
    )
    return ADMIN_HTML.replace("/*__ADMIN_TOKEN__*/", snippet, 1)


__all__ = ["ADMIN_HTML", "render_admin_page"]
