"""Web 本地模式验收自动化：编辑资料 → 捏人 → 通过 → 聊天 → 动态。"""

import json
import os
import time

import httpx
from websockets.sync.client import connect

EP = "http://127.0.0.1:9242"
KEYS = os.environ["W4KEYS"]

pages = httpx.get(EP + "/json/list", timeout=10).json()
t = next(p for p in pages if p["type"] == "page")
ws = connect(t["webSocketDebuggerUrl"], max_size=8 * 1024 * 1024, open_timeout=10)
n = [0]


def call(m, p=None):
    n[0] += 1
    i = n[0]
    ws.send(json.dumps({"id": i, "method": m, "params": p or {}}))
    end = time.time() + 60
    while time.time() < end:
        try:
            msg = json.loads(ws.recv(timeout=3))
        except Exception:
            continue
        if msg.get("id") == i:
            return msg.get("result")
    return None


def ev(e, timeout=30):
    r = call(
        "Runtime.evaluate",
        {"expression": e, "returnByValue": True, "awaitPromise": True, "userGesture": True},
    )
    if not r:
        return {"err": "no-result"}
    if "exceptionDetails" in r:
        return {"err": str(r["exceptionDetails"])[:300]}
    return r["result"].get("value")


def wait(sec):
    time.sleep(sec)


call("Runtime.enable")
call("Log.enable")
call(
    "Page.addScriptToEvaluateOnNewDocument",
    {"source": "window.__errs=[];window.addEventListener('error',e=>window.__errs.push(String(e.message)));window.addEventListener('unhandledrejection',e=>window.__errs.push('REJ:'+String(e.reason)));"},
)
call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True})
call("Page.navigate", {"url": "http://127.0.0.1:8123/"})
wait(8)
print("seed keys:", ev("localStorage.setItem('speakeasy.local.keys.v1', %s); 'ok'" % json.dumps(KEYS)))
call("Page.reload")
wait(9)
print("首页:", (ev("document.body.innerText") or "")[:60].replace("\n", " | "))

# 1) 编辑资料
ev("location.hash='#/me'")
wait(2)
print("点击编辑:", ev("(()=>{const b=document.querySelector('button[aria-label=\"编辑资料\"]'); if(!b) return 'no-btn'; b.click(); return 'clicked'})()"))
wait(1)
print("dialog inputs:", ev("[...document.querySelectorAll('.w4-overlay input')].map(i=>({ph:i.placeholder,type:i.type,val:i.value}))"))
print("dialog buttons:", ev("[...document.querySelectorAll('.w4-overlay button')].map(b=>b.innerText.trim()).filter(Boolean).slice(0,12)"))

# 2) 填名字并保存
print("fill:", ev("""(()=>{
  const ins=[...document.querySelectorAll('.w4-overlay input')];
  const name=ins.find(i=>/名字|昵称|name/i.test(i.placeholder||''))||ins[0];
  if(!name) return 'no-input';
  const setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
  setter.call(name,'Alex'); name.dispatchEvent(new Event('input',{bubbles:true}));
  return 'ok';
})()"""))
print("save:", ev("""(()=>{const b=[...document.querySelectorAll('.w4-overlay button')].find(x=>/保存|完成|确定/.test(x.innerText)); if(!b) return 'no-save-btn'; b.click(); return 'saved'})()"""))
wait(3)
print("保存后 me 文本:", (ev("document.body.innerText") or "")[:100].replace("\n", " | "))

# 3) 等待捏人并看申请
for i in range(20):
    wait(3)
    txt = ev("document.body.innerText") or ""
    if "申请" in txt or "通过" in txt:
        break
ev("location.hash='#/requests'")
wait(3)
print("申请页文本:", (ev("document.body.innerText") or "")[:200].replace("\n", " | "))

# 4) 通过第一个申请
print("accept:", ev("""(()=>{const b=[...document.querySelectorAll('button')].find(x=>/通过|接受/.test(x.innerText)); if(!b) return 'no-accept-btn'; b.click(); return 'accepted'})()"""))
wait(2)
print("通过后:", (ev("document.body.innerText") or "")[:120].replace("\n", " | "))

# 5) 进聊天发消息（从 IndexedDB 读好友 id 后直达聊天页）
friend = ev("""(async()=>{
  const db=await new Promise((res,rej)=>{const r=indexedDB.open('speakeasy',1);r.onsuccess=()=>res(r.result);r.onerror=()=>rej(r.error);});
  const state=await new Promise((res)=>{const tx=db.transaction('kv','readonly');const q=tx.objectStore('kv').get('state.v1');q.onsuccess=()=>res(q.result);});
  const f=(state&&state.friends&&state.friends[0])||null;
  return f?f.id:null;
})()""")
print("好友 id:", (str(friend)[:8] + "…") if friend else None)
if friend:
    ev("location.hash='#/chat/%s'" % friend)
    wait(3)
print("输入框存在:", ev("!!document.querySelector('.w4-composer input, .w4-composer textarea')"))
print("输入框类:", ev("[...document.querySelectorAll('.w4-composer input, .w4-composer textarea')].map(e=>e.tagName+'.'+e.className)"))
print("send text:", ev("""(()=>{
  const ta=document.querySelector('.w4-composer input, .w4-composer textarea');
  if(!ta) return 'no-input';
  const proto = ta.tagName==='TEXTAREA'?window.HTMLTextAreaElement.prototype:window.HTMLInputElement.prototype;
  const setter=Object.getOwnPropertyDescriptor(proto,'value').set;
  setter.call(ta,'Hey! How is your day going?');
  ta.dispatchEvent(new Event('input',{bubbles:true}));
  const b=[...document.querySelectorAll('.w4-composer button')].find(x=>/发送/.test(x.innerText));
  if(!b) return 'no-send';
  b.click(); return 'sent';
})()"""))
wait(22)
print("聊天后文本:", (ev("document.body.innerText") or "")[:320].replace("\n", " | "))

# 6) 动态
ev("location.hash='#/discover/moments'")
wait(3)
print("moments btn:", ev("""(()=>{const b=[...document.querySelectorAll('button')].find(x=>/让好友们发几条|发几条/.test(x.innerText)); if(!b) return 'no-btn'; b.click(); return 'clicked'})()"""))
wait(25)
print("动态页文本:", (ev("document.body.innerText") or "")[:300].replace("\n", " | "))

print("页面错误:", ev("window.__errs && window.__errs.slice(0,8)"))
ws.close()
