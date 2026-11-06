"""给 demo 暴露 dicebearSvgRaw（可传 DiceBear options，例如 skinColor）。"""

import io

p = "frontend/avatar-lab-demo.js"
s = io.open(p, encoding="utf-8").read()

old = "window.AvatarLab = { styles: Object.keys(STYLES), custom: Object.keys(CUSTOM), dicebearSvg,"
new = '''function dicebearSvgRaw(name, seed, opts) {
  const style = STYLES[name];
  if (!style) return "";
  return createAvatar(style, Object.assign({ seed: String(seed) }, opts || {})).toString();
}

window.AvatarLab = { styles: Object.keys(STYLES), custom: Object.keys(CUSTOM), dicebearSvg, dicebearSvgRaw,'''
assert old in s, "export anchor not found"
s = s.replace(old, new, 1)
io.open(p, "w", encoding="utf-8").write(s)
print("dicebearSvgRaw added:", "dicebearSvgRaw" in s)
