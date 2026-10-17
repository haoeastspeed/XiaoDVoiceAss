/* 无头单元测试：stub 最小 DOM + Canvas 2D 上下文，跑 avatar.js 的
   **渐变圆脸"声波小人"**（参考用户风格图：渐变球 + 圆点眼 + 弧线眉 +
   微笑/口型；全部 canvas 逐帧重画，无照片无贴片）。

   跑法：
     node tests/test_avatar_render.js        （退出码 0 = 全通过）

   覆盖：
     1. 挂载：canvas + 覆盖层 SVG 各一份，坐标为 CSS 像素×dpr
     2. 绘制不产生 NaN/Infinity（NaN 会让整块画布变空白）
     3. 球体几何：半径 106 的圆 + 对角渐变 + 左上高光，脸被裁进球内
     4. 静止也动：呼吸/摆动/凝视逐帧变化（不是静态贴图）
     5. 眨眼：10 秒内至少眨一次且幅度完整，之后睁回；force.blink 可钉帧
     6. 口型：speaking + 电平驱动张嘴，转回 idle 后闭合
     7. 换形象：音色 id → 配色（男女、互不相同、同一 id 稳定）
     8. 状态机：非法状态回落 idle；class 只留当前状态一个
     9. freeze：rAF 停摆（预览/测试钉帧用）
     10. 运动量界：长时间运行头转动不发散
*/
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const AVATAR_JS = path.resolve(__dirname, "..", "app", "ui", "avatar.js");

/* ---------------- 断言 ---------------- */
let pass = 0;
const fails = [];
function ok(name, cond, extra) {
  if (cond) { pass++; console.log("  ok   " + name); }
  else { fails.push(name); console.log("  FAIL " + name + (extra ? " — " + extra : "")); }
}
function eq(name, got, want) { ok(name, got === want, `got=${got} want=${want}`); }

/* ---------------- DOM stub ---------------- */
function makeEl(tag) {
  const el = {
    tagName: tag, attrs: {}, children: [], style: {}, textContent: "",
    _cls: new Set(), width: 0, height: 0, className: "",
    setAttribute(k, v) {
      if (k === "class") this._cls = new Set(String(v).split(/\s+/).filter(Boolean));
      this.attrs[k] = String(v);
    },
    getAttribute(k) { return this.attrs[k] || ""; },
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    insertBefore(n) { this.children.unshift(n); n.parentNode = this; return n; },
    removeChild(c) {
      const i = this.children.indexOf(c);
      if (i >= 0) this.children.splice(i, 1);
      return c;
    },
    get firstChild() { return this.children[0] || null; },
    getBoundingClientRect() { return { width: 420, height: 420 }; },
    addEventListener() {},
    getContext() { return (this._ctx = makeCtx()); },
  };
  el.classList = {
    add: (c) => String(c).split(/\s+/).forEach((x) => el._cls.add(x)),
    remove: (c) => String(c).split(/\s+/).forEach((x) => el._cls.delete(x)),
    contains: (c) => el._cls.has(c),
  };
  Object.defineProperty(el, "classList", { value: el.classList });
  return el;
}

/* 记录型 2D 上下文：把所有数值参数收集起来方便查 NaN；
   渐变对象记录 colorStop 供配色断言 */
const NUMS = { bad: [] };
const GRADS = [];
function makeCtx() {
  const rec = { calls: [], args: [] };
  const note = (name, args) => {
    rec.calls.push(name);
    args.forEach((a, i) => {
      if (typeof a === "number" && !Number.isFinite(a)) {
        NUMS.bad.push(`${name}#${i}=${a}`);
      }
    });
    rec.args.push(args);
  };
  const grad = (kind) => {
    const stops = [];
    const g = { addColorStop: (o, c) => stops.push([o, String(c)]) };
    Object.defineProperty(g, "_stops", { value: stops });
    Object.defineProperty(g, "_kind", { value: kind });
    GRADS.push(g);
    return g;
  };
  const ctx = {
    fillStyle: "", strokeStyle: "", lineWidth: 1, lineCap: "butt", lineJoin: "miter",
    globalAlpha: 1, globalCompositeOperation: "source-over", font: "",
    shadowBlur: 0, shadowColor: "", filter: "",
    _rec: rec,
    save() { note("save", [...arguments]); },
    restore() { note("restore", [...arguments]); },
    beginPath() { note("beginPath", [...arguments]); },
    closePath() { note("closePath", [...arguments]); },
    moveTo() { note("moveTo", [...arguments]); },
    lineTo() { note("lineTo", [...arguments]); },
    arc() { note("arc", [...arguments]); },
    ellipse() { note("ellipse", [...arguments]); },
    quadraticCurveTo() { note("quadraticCurveTo", [...arguments]); },
    bezierCurveTo() { note("bezierCurveTo", [...arguments]); },
    rect() { note("rect", [...arguments]); },
    fill() { note("fill", [...arguments]); },
    stroke() { note("stroke", [...arguments]); },
    clip() { note("clip", [...arguments]); },
    fillRect() { note("fillRect", [...arguments]); },
    clearRect() { note("clearRect", [...arguments]); },
    translate() { note("translate", [...arguments]); },
    scale() { note("scale", [...arguments]); },
    rotate() { note("rotate", [...arguments]); },
    setTransform() { note("setTransform", [...arguments]); },
    drawImage() { note("drawImage", [...arguments]); },
    createLinearGradient() { note("createLinearGradient", [...arguments]); return grad("linear"); },
    createRadialGradient() { note("createRadialGradient", [...arguments]); return grad("radial"); },
    createPattern() { return null; },
    measureText() { return { width: 10 }; },
    fillText() { note("fillText", [...arguments]); },
  };
  return ctx;
}

function buildSandbox() {
  // 固定随机序列：眨眼/视线都用到 Math.random，测试要可复现
  let seed = 12345;
  const rnd = () => {
    seed = (seed * 1103515245 + 12345) & 0x7fffffff;
    return seed / 0x7fffffff;
  };

  const wrap = makeEl("div");
  const avatar = makeEl("div");
  avatar.id = "avatar";
  wrap.appendChild(avatar);
  const stateText = makeEl("div");
  stateText.id = "stateText";

  const byId = { avatar: avatar, stateText: stateText };
  const document = {
    readyState: "complete",
    getElementById: (id) => byId[id] || null,
    createElement: (t) => makeEl(t),
    createElementNS: (_ns, t) => makeEl(t),
    addEventListener() {},
    body: makeEl("body"),
  };
  const rafQueue = [];
  const window = {
    devicePixelRatio: 2,
    addEventListener() {},
    requestAnimationFrame: (cb) => { rafQueue.push(cb); return rafQueue.length; },
  };
  const sandbox = {
    document, window, console,
    Math: Object.assign(Object.create(Math), { random: rnd }),
    requestAnimationFrame: window.requestAnimationFrame,
    cancelAnimationFrame() {},
    performance: { now: () => 0 },
    Date, JSON, Number, String, Object, Array, Boolean, Error, parseInt, parseFloat,
    setTimeout, clearTimeout, isNaN,
  };
  sandbox.window.addEventListener = () => {};
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(AVATAR_JS, "utf-8"), sandbox,
                  { filename: AVATAR_JS });
  return { sandbox, wrap, avatar, stateText, rafQueue };
}

/* ---------------- 开跑 ---------------- */
console.log("== 渐变圆脸声波小人（Canvas 逐帧绘制） ==");
const env = buildSandbox();
const Avatar = env.sandbox.window.Avatar;
ok("Avatar 单例已挂到 window", !!Avatar && typeof Avatar.apply === "function");

const canvas = env.avatar.children.find((c) => c.tagName === "canvas");
ok("容器里创建了 canvas", !!canvas);
ok("canvas 类名 av-canvas", !!canvas && canvas.className === "av-canvas");
eq("canvas 像素尺寸 = 420×dpr(2)", canvas && canvas.width, 840);
ok("首帧已绘制（不至于空白）", !!canvas && canvas._ctx && canvas._ctx._rec.calls.length > 20,
   canvas && canvas._ctx ? "calls=" + canvas._ctx._rec.calls.length : "");

const overlay = env.wrap.children.find((c) => c.tagName === "svg");
ok("覆盖层 SVG 挂在 wrap 上（不是 canvas 里）", !!overlay);
ok("覆盖层有声波/思考点/搜索标记",
   !!overlay && overlay.children.length >= 3);

// ---- 1. 全状态绘制无 NaN ----
NUMS.bad.length = 0;
const STATES = ["idle", "starting", "listening", "thinking", "searching",
                "speaking", "off"];
STATES.forEach((s) => {
  for (let lv = 0; lv <= 1.0001; lv += 0.25) {
    Avatar.pose(s, Math.min(1, lv), 4);
  }
});
ok("7 个状态 × 5 档电平绘制无 NaN/Infinity", NUMS.bad.length === 0,
   NUMS.bad.slice(0, 3).join(", "));

// ---- 2. 球体几何与结构 ----
{
  const rec = canvas._ctx._rec;
  // 球体：arc(150,150,106,...)（圆心/半径即绘制坐标，transform 另行叠加）
  const ballArc = rec.args.findIndex((a) =>
    a.length >= 3 && Math.abs(a[0] - 150) < 0.01 &&
    Math.abs(a[1] - 150) < 0.01 && Math.abs(a[2] - 106) < 0.01);
  ok("画了半径 106 的球体", ballArc >= 0,
     ballArc >= 0 ? "" : "arc args=" + JSON.stringify(rec.args.slice(0, 6)));
  // 脸被裁进球内：clip 之前应有一个半径 ~105 的圆
  let clipIdx = -1;
  for (let i = 0; i < rec.calls.length; i++) {
    if (rec.calls[i] === "clip") { clipIdx = i; break; }
  }
  ok("脸部绘制有 clip（裁进球内）", clipIdx > ballArc);
  const clipArc = rec.args.slice(0, clipIdx).reverse().find((a) =>
    a.length >= 3 && Math.abs(a[2] - 105) < 0.6);
  ok("clip 圆略小于球体（r≈105）", !!clipArc);
  // 渐变：对角 linear（球体）+ radial（高光）
  GRADS.length = 0;
  Avatar.pose("idle", 0, 1);
  ok("球体用对角线性渐变",
     GRADS.some((g) => g._kind === "linear"), "grads=" + GRADS.length);
  ok("高光用径向渐变", GRADS.some((g) => g._kind === "radial"));
}

// ---- 3. 配色随音色切换 ----
function gradColorsFor(voice, state) {
  GRADS.length = 0;
  if (voice) Avatar.setVoice(voice);
  Avatar.pose(state || "idle", 0, 2);
  const lin = GRADS.filter((g) => g._kind === "linear" && g._stops.length >= 2);
  return lin.length ? lin[lin.length - 1]._stops.map((s) => s[1]) : [];
}
const cA = gradColorsFor("zh-CN-XiaoxiaoNeural");
ok("晓晓配色含 c1/c2 两档", cA.length >= 2, cA.join(","));
ok("晓晓渐变起点是 #45c4ff", cA[0] === "#45c4ff", "got " + cA[0]);
const cB = gradColorsFor("zh-CN-YunjianNeural");
ok("云健配色与晓晓不同", cB[0] !== cA[0] || cB[1] !== cA[1],
   cB.join(","));
const cJ = gradColorsFor("en-US-JennyNeural");
ok("Jenny 配色又不同", cJ[0] !== cB[0], cJ.join(","));
gradColorsFor(null);   // 回到当前形象继续画，不影响后续断言

// ---- 4. 静止状态也在动（呼吸/微动） ----
Avatar.pose("idle", 0, 1);
const d1 = Avatar.debug();
Avatar.pose(null, null, 24);          // 0.4s
const d2 = Avatar.debug();
ok("呼吸/摆动逐帧变化（不是静态贴图）",
   Math.abs(d1.breath - d2.breath) > 1e-3,
   `breath ${d1.breath.toFixed(4)}→${d2.breath.toFixed(4)}`);

// ---- 5. 眨眼：10s 内至少一次，且能完全睁开 ----
let blinked = false, maxBlink = 0;
for (let i = 0; i < 600; i++) {          // 600 帧 @1/60 = 10s
  Avatar.pose(null, null, 1);
  const d = Avatar.debug();
  if (d.blink > 0.05) blinked = true;
  maxBlink = Math.max(maxBlink, d.blink);
}
ok("10 秒内眨过眼", blinked);
ok("眨眼有完整幅度（不是永远半眯）", maxBlink > 0.8, "max=" + maxBlink.toFixed(2));
Avatar.pose(null, null, 120);   // 末尾可能触发"双眨"，等整条链走完
ok("眨眼后会睁回去", Avatar.debug().blink < 0.05,
   "blink=" + Avatar.debug().blink.toFixed(3));

// force.blink 钉帧（预览/截图验证用）
NUMS.bad.length = 0;
Avatar.pose("idle", 0, 2, { blink: 1 });
const dbgPin = Avatar.debug();
ok("pose force.blink 钉住（debug 可读）", Math.abs(dbgPin.blink - 1) < 1e-9,
   "blink=" + dbgPin.blink);
ok("全闭眼绘制无 NaN", NUMS.bad.length === 0, NUMS.bad.slice(0, 3).join(", "));
Avatar.pose("idle", 0, 2, { blink: 0.5 });
ok("半闭眼也能钉住", Math.abs(Avatar.debug().blink - 0.5) < 1e-9);
Avatar.pose(null, null, 120);   // 钉 0.5 会重启一次眨眼周期，等它走完
ok("钉帧后继续跑会恢复自然眨眼周期", Avatar.debug().blink < 0.05,
   "blink=" + Avatar.debug().blink.toFixed(3));

// ---- 6. 口型：说话时张嘴，闭麦后合上 ----
Avatar.pose("speaking", 0, 1);
let openMax = 0;
for (let i = 0; i < 120; i++) {
  Avatar.level(0.9);
  Avatar.pose(null, null, 1);
  openMax = Math.max(openMax, Avatar.debug().open);
}
ok("说话时嘴会张开", openMax > 0.15, "openMax=" + openMax.toFixed(3));
Avatar.level(0.9);
Avatar.pose("idle", 0, 90);
ok("停止说话 1.5s 后嘴闭合", Avatar.debug().open < 0.02,
   "open=" + Avatar.debug().open.toFixed(4));

// ---- 7. 换形象（音色 → 形象） ----
const lookOf = (id) => { Avatar.setVoice(id); return Avatar.char; };
const female = lookOf("zh-CN-XiaoxiaoNeural");
Avatar.setVoice("zh-CN-YunjianNeural");
const male = Avatar.char;
ok("存在女声形象", !!female && female.gender === "f",
   female && female.gender);
ok("存在男声形象", !!male && male.gender === "m", male && male.gender);
ok("男女切换生效", male !== female);
Avatar.setVoice("zh-CN-XiaoxiaoNeural");
ok("同一个音色映射到同一个形象（稳定）", Avatar.char === female);
Avatar.setVoice("local:zm_yunxi");
ok("local: 前缀音色也能匹配形象", Avatar.char.gender === "m",
   Avatar.char.gender + "/" + Avatar.char.name);

// 不同女声要有不同配色（不是所有女声都一个样）
const palettes = new Set();
["zh-CN-XiaoxiaoNeural", "zh-CN-XiaoyiNeural", "zh-CN-shaanxi-XiaoniNeural",
 "zh-CN-liaoning-XiaobeiNeural", "en-US-JennyNeural"].forEach((v) => {
  palettes.add(lookOf(v).c1);
});
ok("不同女声配色有区分度", palettes.size >= 3, [...palettes].join(","));

// ---- 8. 状态回落与互斥 class ----
Avatar.apply("not-a-state", "自定义文本");
eq("非法状态回落到 idle", Avatar.state, "idle");
eq("状态文案已写入", env.stateText.textContent, "自定义文本");
Avatar.apply("listening");
ok("wrap 上挂着 s-listening", env.wrap.classList.contains("s-listening"));
Avatar.apply("speaking");
ok("切状态后旧 class 被清掉", !env.wrap.classList.contains("s-listening"));

// ---- 9. 运动量界：长时间运行不发散 ----
NUMS.bad.length = 0;
let maxHeadR = 0;
for (let i = 0; i < 1800; i++) {          // 30s
  Avatar.pose("speaking", 0.8, 1);
  maxHeadR = Math.max(maxHeadR, Math.abs(Avatar.debug().headR));
}
ok("30s 连续运行无 NaN", NUMS.bad.length === 0, NUMS.bad.slice(0, 3).join(", "));
ok("摆头幅度有界（不发散）", maxHeadR < 15, "max|headR|=" + maxHeadR.toFixed(2));
STATES.forEach((s) => Avatar.pose(s, 0.6, 3));
ok("收尾复跑全状态仍无 NaN", NUMS.bad.length === 0, NUMS.bad.slice(0, 3).join(", "));

// ---- 10. freeze：钉帧后 rAF 循环停摆（预览/测试用） ----
const q1 = env.rafQueue.length;
ok("正常时有 rAF 排队", q1 > 0, "queue=" + q1);
Avatar.freeze();
env.rafQueue.splice(0).forEach((cb) => cb());     // 消费已排队帧
eq("freeze 后 rAF 不再排队", env.rafQueue.length, 0);
Avatar.pose("idle", 0, 2);
ok("freeze 不影响 pose 手动摆帧", !!canvas && canvas._ctx._rec.calls.length > 0);

/* ---------------- 汇总 ---------------- */
console.log("");
if (fails.length) {
  console.log(`AVATAR_FAIL ${fails.length}/${pass + fails.length}`);
  fails.forEach((f) => console.log("   - " + f));
  process.exit(1);
}
console.log(`AVATAR_PASS ${pass}/${pass}`);
