/* 把 avatar.js 生成的 8 个角色序列化成独立 SVG 文件，供视觉走查 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

function makeEl(tag) {
  const el = {
    tagName: tag, attrs: {}, children: [], style: {}, textContent: "",
    _cls: new Set(),
    setAttribute(k, v) {
      if (k === "class") this._cls = new Set(String(v).split(/\s+/).filter(Boolean));
      this.attrs[k] = String(v);
    },
    getAttribute(k) { return this.attrs[k]; },
    appendChild(c) { this.children.push(c); return c; },
    removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); return c; },
    get firstChild() { return this.children[0] || null; },
    addEventListener() {}, removeEventListener() {},
    classList: {
      add(...cs) { cs.forEach((c) => el._cls.add(c)); },
      remove(...cs) { cs.forEach((c) => el._cls.delete(c)); },
      contains(c) { return el._cls.has(c); },
    },
    set className(v) { el.setAttribute("class", v); },
    get className() { return [...el._cls].join(" "); },
  };
  return el;
}
const nodes = {};
function getById(id) { return nodes[id] || (nodes[id] = makeEl("div")); }

const sandbox = {
  document: {
    readyState: "complete",
    createElementNS: (ns, tag) => makeEl(tag),
    getElementById: getById,
    addEventListener() {},
  },
  requestAnimationFrame: () => 1, setTimeout: () => 0, clearTimeout: () => {},
  window: {}, console,
};
sandbox.window = sandbox;
vm.createContext(sandbox);
const AVATAR_JS = path.resolve(__dirname, "..", "app", "ui", "avatar.js");
vm.runInContext(fs.readFileSync(AVATAR_JS, "utf8"), sandbox,
  { filename: "avatar.js" });

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function ser(n) {
  let s = "<" + n.tagName;
  for (const k in n.attrs) s += " " + k + '="' + esc(n.attrs[k]) + '"';
  if (n._cls.size) s += ' class="' + [...n._cls].join(" ") + '"';
  if (n.style && Object.keys(n.style).length)
    s += ' style="' + Object.entries(n.style)
      .map(([k, v]) => k + ":" + v).join(";") + '"';
  if (!n.children.length && !n.textContent) return s + "/>";
  s += ">";
  if (n.textContent) s += esc(n.textContent);
  n.children.forEach((c) => { s += ser(c); });
  return s + "</" + n.tagName + ">";
}

const Avatar = sandbox.window.Avatar;
const svg = nodes["avatar"];
const CHARS = ["xiaoxiao", "yunxi", "hsiaochen", "yunjhe", "jenny", "mark",
               "zhiyu", "nana"];
const out = [];
for (const id of CHARS) {
  Avatar.setVoice("force-" + id);
  const byId = {};
  (function walk(n) { byId[n.tagName] = n; n.children.forEach(walk); })(svg);
  // 直接按 id 找：重建后 CHARS 顺序即 char 顺序
  out.push({ id, name: Avatar.char.name, svg: ser(svg) });
}
// 上面 setVoice 对未知 id 走 guessChar，顺序不等于 CHARS；改为按名字筛
// —— 重新逐个精确设置：用 VOICE_MAP 反查
const VOICE_BY_CHAR = {
  xiaoxiao: "zh-CN-XiaoxiaoNeural", yunxi: "zh-CN-YunxiNeural",
  hsiaochen: "zh-TW-HsiaoChenNeural", yunjhe: "zh-TW-YunJheNeural",
  jenny: "en-US-JennyNeural", mark: "en-US-GuyNeural",
  zhiyu: "zz", nana: "zh-CN-XiaohanNeural",
};
const cards = CHARS.map((id) => {
  Avatar.setVoice(VOICE_BY_CHAR[id]);
  // 垫片里 #avatar 是裸 div，序列化时补回真 <svg> 的属性
  const markup = ser(svg).replace(/^<div/, "<svg")
    .replace(/<\/div>$/, "</svg>");
  return {
    id, name: Avatar.char.name,
    svg: '<svg class="avatar" viewBox="0 0 260 260" '
      + 'xmlns="http://www.w3.org/2000/svg">'
      + markup.replace(/^<svg[^>]*>/, "").replace(/<\/svg>$/, "")
      + "</svg>",
  };
});
fs.writeFileSync(path.resolve(__dirname, "..", ".tmp", "avatar_cards.json"),
  JSON.stringify(cards));
console.log("generated", cards.map((c) => c.name).join(" / "));
