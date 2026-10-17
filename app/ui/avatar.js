/* 虚拟形象：渐变圆脸"声波小人"（参考用户提供的风格图）

  形象 = 一颗对角渐变的圆球（每个音色一组专属配色）+ 圆点眼睛 +
  弧线眉 + 微笑/口型，两侧声波与思考点由覆盖层 SVG 承担（CSS 按状态类
  驱动动画，见 style.css）。整个角色全部由 canvas 逐帧重画——没有照片、
  没有分层贴片，因此不存在"图片上打补丁"的割裂感，任何动画都是连续的。

  动画（dt 积分，帧率无关）：
    * 呼吸/漂浮 —— 球体微缩放 + 低频平移，多正弦叠加不重复
    * 摆动/点头 —— 绕枢轴微转（说话时随音节点头、听时侧倾、想时歪头）
    * 眨眼      —— 眼睛垂直压扁至缝线，随机双眨
    * 视线      —— 眼球整体小幅扫视（思考时看向上方）
    * 口型      —— 播放电平驱动：微笑弧线 → 开合椭圆 + 舌头，元音圆/扁唇交替

  对外契约（app.js 依赖，别改）：
    Avatar.apply(state, text)  状态机
    Avatar.level(0~1)          播放电平驱动口型（仅 speaking 生效）
    Avatar.setVoice(voiceId)   音色 → 形象（选配色）
    Avatar.pose(state, level, steps, force)  离线渲染一帧（截图/测试用）
    Avatar.freeze()            停 rAF（预览/测试钉帧用）
    Avatar.debug()             内部动画量（测试用）
    window.Avatar.state / .char
  CSS 契约：#avatar 与 wrap 上的 s-* 状态类、.av-over 覆盖 SVG
            （.wave/.tdot/.searchMark 由本文件创建，样式在 style.css）
*/
(function () {
  "use strict";

  const STATES = ["idle", "starting", "listening", "thinking", "searching",
                  "speaking", "off"];
  const TEXT = {
    idle: "待机中 · 点「开始聊天」",
    starting: "正在准备…",
    listening: "正在聆听，请说话",
    thinking: "小D 正在想…",
    searching: "正在联网搜索…",
    speaking: "小D 正在说…",
    off: "已结束",
  };
  const NS = "http://www.w3.org/2000/svg";
  const VP = 300;                 // 逻辑画布边长（绘制坐标恒定，缩放交给 ctx）

  /* ---------- 形象库：每个音色一组渐变配色 c1→c2 ---------- */
  const LOOKS = {
    xiaoxiao: { name: "晓晓", gender: "f", lang: "zh-CN",
                c1: "#45c4ff", c2: "#7c5cff" },
    xiaoyi:   { name: "晓伊", gender: "f", lang: "zh-CN",
                c1: "#ff8fc8", c2: "#8b5cf6" },
    xiaobei:  { name: "晓北", gender: "f", lang: "zh-CN",
                c1: "#3ed9b5", c2: "#3b82f6" },
    xiaoni:   { name: "晓妮", gender: "f", lang: "zh-CN",
                c1: "#ffb86c", c2: "#ff6c9d" },
    yunjian:  { name: "云健", gender: "m", lang: "zh-CN",
                c1: "#64d2ff", c2: "#4f6bff" },
    yunyang:  { name: "云扬", gender: "m", lang: "zh-CN",
                c1: "#7ee08a", c2: "#2fa8d5" },
    yunxia:   { name: "云夏", gender: "m", lang: "zh-CN",
                c1: "#b48cff", c2: "#6366f1" },
    jenny:    { name: "Jenny", gender: "f", lang: "en-US",
                c1: "#ff9db0", c2: "#b06cff" },
  };

  /* 音色 ID → 形象。没列到的走 guessLook（按性别/语言猜）。 */
  const VOICE_MAP = {
    "zh-CN-XiaoxiaoNeural": "xiaoxiao",
    "zh-CN-XiaoyiNeural": "xiaoyi",
    "zh-CN-XiaohanNeural": "xiaoni",
    "zh-CN-XiaomoNeural": "xiaoxiao",
    "zh-CN-XiaoqiuNeural": "xiaoni",
    "zh-CN-XiaoruiNeural": "xiaoni",
    "zh-CN-XiaoshuangNeural": "xiaoyi",
    "zh-CN-XiaochenNeural": "xiaoyi",
    "zh-CN-XiaohanNeural2": "xiaoni",
    "zh-CN-liaoning-XiaobeiNeural": "xiaobei",
    "zh-CN-shaanxi-XiaoniNeural": "xiaoni",
    "zh-CN-YunjianNeural": "yunjian",
    "zh-CN-YunxiNeural": "yunxia",
    "zh-CN-YunxiaNeural": "yunxia",
    "zh-CN-YunyangNeural": "yunyang",
    "zh-CN-YunyeNeural": "yunyang",
    "zh-CN-YunzeNeural": "yunjian",
    "zh-TW-HsiaoChenNeural": "xiaoyi",
    "zh-TW-HsiaoYuNeural": "xiaoni",
    "zh-TW-YunJheNeural": "yunyang",
    "zh-HK-HiuGaaiNeural": "xiaoyi",
    "zh-HK-HiuMaanNeural": "xiaoni",
    "zh-HK-WanLungNeural": "yunyang",
    "en-US-JennyNeural": "jenny",
    "en-US-AriaNeural": "jenny",
    "en-US-AnaNeural": "jenny",
    "en-US-MichelleNeural": "jenny",
    "en-US-GuyNeural": "yunyang",
    "en-US-DavisNeural": "yunyang",
    "en-US-RogerNeural": "yunjian",
    "en-GB-SoniaNeural": "jenny",
    "en-GB-LibbyNeural": "jenny",
    "en-GB-RyanNeural": "yunyang",
    "ja-JP-NanamiNeural": "xiaoyi",
    "ko-KR-SunHiNeural": "xiaoyi",
    // 本地神经音色（Kokoro 的 id）也映射到同一批外观
    zf_xiaoxiao: "xiaoxiao", zf_xiaoyi: "xiaoyi", zf_xiaobei: "xiaobei",
    zf_xiaoni: "xiaoni", zm_yunjian: "yunjian", zm_yunxi: "yunxia",
    zm_yunxia: "yunxia", zm_yunyang: "yunyang",
    af_heart: "jenny", af_bella: "jenny", af_sky: "xiaoyi",
    am_adam: "yunyang", am_liam: "yunjian", am_michael: "yunxia",
    bf_emma: "jenny", bm_george: "yunyang",
  };
  const FEMALE_TOKENS = ["xiaoxiao", "xiaoyi", "xiaohan", "xiaomo", "xiaorui",
    "xiaoshuang", "xiaochen", "xiaoni", "xiaobei", "hsiaochen", "hsiaoyu",
    "hiugaai", "hiumaan", "jenny", "aria", "ana", "michelle", "sonia",
    "libby", "nanami", "sunhi", "huihui", "hanhan", "yaoyao", "tracy",
    "tingting", "zira", "hazel", "susan", "eva", "lili", "meng",
    "zf_", "af_", "bf_"];
  const MALE_TOKENS = ["yunxi", "yunjian", "yunyang", "yunjhe", "yunye",
    "yunze", "guy", "davis", "roger", "ryan", "kangkang", "danny", "david",
    "yunjie", "wei", "wanlung", "zm_", "am_", "bm_"];

  function hash(s) {
    let h = 2166136261;
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = (h * 16777619) >>> 0;
    }
    return h;
  }

  function guessLook(voiceId) {
    const id = String(voiceId || "").toLowerCase();
    let gender = "";
    if (FEMALE_TOKENS.some((t) => id.includes(t))) gender = "f";
    else if (MALE_TOKENS.some((t) => id.includes(t))) gender = "m";
    let lang = "zh-CN";
    if (id.includes("zh-tw") || id.includes("繁体")) lang = "zh-TW";
    else if (id.includes("en-") || id.includes("英语") || id.includes("english"))
      lang = "en-US";
    let pool = Object.keys(LOOKS).filter((k) => !gender ||
      LOOKS[k].gender === gender);
    if (!pool.length) pool = Object.keys(LOOKS).slice();
    const sameLang = pool.filter((k) => LOOKS[k].lang === lang);
    if (sameLang.length) pool = sameLang;
    return LOOKS[pool[hash(id || "default") % pool.length]];
  }

  /* ---------- 小工具 ---------- */
  const clamp = (v, a, b) => (v < a ? a : (v > b ? b : v));
  const smooth = (x) => x * x * (3 - 2 * x);
  // 指数平滑：帧率无关（tau＝时间常数，秒）
  const approach = (cur, target, dt, tau) =>
    cur + (target - cur) * (1 - Math.exp(-dt / Math.max(0.001, tau)));

  function circle(g, x, y, r) {
    g.beginPath();
    g.arc(x, y, r, 0, Math.PI * 2);
  }
  function ell(g, x, y, rx, ry, rot) {
    g.beginPath();
    if (g.ellipse) { g.ellipse(x, y, rx, ry, rot || 0, 0, Math.PI * 2); return; }
    const k = 0.5523, c = Math.cos(rot || 0), s = Math.sin(rot || 0);
    const px = (dx, dy) => [x + dx * c - dy * s, y + dx * s + dy * c];
    const pts = [[rx, 0], [0, ry], [-rx, 0], [0, -ry]];
    const ctl = [[rx, ry * k], [rx * k, ry], [-rx * k, ry], [-rx, ry * k],
                 [-rx, -ry * k], [-rx * k, -ry], [rx * k, -ry], [rx, -ry * k]];
    let p = px(pts[0][0], pts[0][1]);
    g.moveTo(p[0], p[1]);
    for (let i = 0; i < 4; i++) {
      const a = px(ctl[i * 2][0], ctl[i * 2][1]);
      const b = px(ctl[i * 2 + 1][0], ctl[i * 2 + 1][1]);
      const e = px(pts[(i + 1) % 4][0], pts[(i + 1) % 4][1]);
      g.bezierCurveTo(a[0], a[1], b[0], b[1], e[0], e[1]);
    }
    g.closePath();
  }

  /* ---------- 动画状态 ---------- */
  const A = {
    t: 0,
    breath: 0,          // -1..1
    level: 0,           // 目标电平
    levelS: 0,          // 平滑后的电平
    open: 0,            // 嘴张开 0..1
    round: 0.5, wide: 0.5,
    sylPhase: 0, sylF: 4.6, visPhase: 0,
    blink: 0, blinking: false, blinkP: 0, blinkDur: 0.3,
    blinkT: 0, blinkNext: 2.4, dbl: false,
    gazeX: 0, gazeY: 0, gazeTX: 0, gazeTY: 0, gazeT: 0, gazeNext: 1.6,
    headR: 0, headX: 0, headY: 0,
    brow: 0, smile: 0.22,
  };

  let cur = "idle";
  let look = LOOKS.xiaoxiao;
  let container = null, wrapEl = null, canvas = null, ctx = null;
  let cssSize = VP, dpr = 1;

  /* ---------- 更新（dt 积分，帧率无关） ---------- */
  function update(dt) {
    A.t += dt;

    // 呼吸：说话时更快更浅，思考时慢
    const period = cur === "speaking" ? 2.3
      : (cur === "thinking" ? 5.4 : (cur === "listening" ? 3.4 : 4.2));
    A.breath = Math.sin(2 * Math.PI * A.t / period);

    // 电平：攻击快、释放慢
    const tau = A.level > A.levelS ? 0.055 : 0.15;
    A.levelS = approach(A.levelS, A.level, dt, tau);
    if (cur !== "speaking") A.levelS = approach(A.levelS, 0, dt, 0.12);

    // 音节：让嘴在电平平稳时也一开一合（真实说话的节律）
    A.sylF += (Math.random() - 0.5) * dt * 2.2;
    A.sylF = clamp(A.sylF, 3.4, 6.4);
    A.sylPhase += dt * A.sylF;
    const syl = 0.5 - 0.5 * Math.cos(2 * Math.PI * A.sylPhase);

    const wantOpen = (cur === "speaking")
      ? clamp(A.levelS, 0, 1) * (0.28 + 0.72 * syl) : 0;
    A.open = approach(A.open, wantOpen, dt, wantOpen > A.open ? 0.038 : 0.075);

    // 元音口型：圆唇 / 扁唇慢慢交替（不是随机跳变）
    A.visPhase += dt * (1.05 + A.levelS * 1.15);
    A.round = 0.5 + 0.5 * Math.sin(2 * Math.PI * A.visPhase);
    A.wide = 0.5 + 0.5 * Math.sin(2 * Math.PI * A.visPhase * 0.63 + 1.1);

    // 眨眼
    if (A.blinking) {
      A.blinkP += dt / A.blinkDur;
      if (A.blinkP >= 1) {
        A.blinking = false; A.blinkP = 0; A.blink = 0;
        if (A.dbl) { A.dbl = false; A.blinkNext = 0.28; }   // 连眨第二下
        else A.blinkNext = 2.2 + Math.random() * 3.8;
        A.blinkT = 0;
      } else {
        const p = A.blinkP;
        A.blink = p < 0.36 ? smooth(p / 0.36)
                           : 1 - smooth((p - 0.36) / 0.64);
      }
    } else {
      A.blinkT += dt;
      if (A.blinkT >= A.blinkNext && cur !== "off") {
        A.blinking = true; A.blinkP = 0;
        A.blinkDur = 0.26 + Math.random() * 0.14;
        A.dbl = Math.random() < 0.14;
      }
    }

    // 视线扫视（驱动眼球整体微移；思考时看向上方）
    A.gazeT += dt;
    if (A.gazeT >= A.gazeNext) {
      A.gazeT = 0;
      A.gazeNext = 1.2 + Math.random() * 2.2;
      if (cur === "thinking") { A.gazeTX = -0.55; A.gazeTY = -0.75; }
      else if (cur === "listening") {
        A.gazeTX = (Math.random() - 0.5) * 0.5; A.gazeTY = 0.05;
      } else if (cur === "speaking") {
        A.gazeTX = (Math.random() - 0.5) * 1.3;
        A.gazeTY = (Math.random() - 0.5) * 0.7;
      } else {
        A.gazeTX = (Math.random() - 0.5) * 0.9;
        A.gazeTY = (Math.random() - 0.5) * 0.5;
      }
    }
    A.gazeX = approach(A.gazeX, A.gazeTX, dt, 0.10);
    A.gazeY = approach(A.gazeY, A.gazeTY, dt, 0.10);

    // 摆动：低频摇摆 + 说话点头 + 状态侧倾
    const sway = 1.7 * Math.sin(2 * Math.PI * A.t / 7.1)
               + 0.7 * Math.sin(2 * Math.PI * A.t / 2.93);
    const nod = (cur === "speaking" && A.levelS > 0.03)
      ? 2.6 * A.open * Math.sin(2 * Math.PI * A.sylPhase) : 0;
    const tilt = cur === "listening" ? -1.6
      : (cur === "thinking" ? 2.8 : (cur === "speaking" ? 0.5 : 0));
    A.headR = sway + nod + tilt;
    A.headX = 1.1 * Math.sin(2 * Math.PI * A.t / 6.7)
            + 0.5 * Math.sin(2 * Math.PI * A.t / 3.3);
    A.headY = 1.0 * Math.sin(2 * Math.PI * A.t / 5.3) - A.breath * 0.9
            - (cur === "speaking" ? A.open * 0.7 : 0);

    // 眉毛 / 微笑
    let browT = 0.10 + A.open * 0.55 + A.breath * 0.03;
    if (cur === "listening") browT += 0.28;          // 认真听
    if (cur === "thinking") browT += 0.34;
    A.brow = approach(A.brow, browT, dt, 0.12);
    const smileT = cur === "speaking" ? 0.30 + 0.30 * A.open
      : (cur === "listening" ? 0.34 : 0.20);
    A.smile = approach(A.smile, smileT, dt, 0.20);
  }

  /* ---------- 画布 ---------- */
  function resize() {
    if (!canvas || !container) return;
    let w = VP;
    try {
      const r = container.getBoundingClientRect();
      if (r && r.width > 10) w = r.width;
    } catch (e) { /* 测试环境没有布局 */ }
    cssSize = w;
    dpr = Math.min(2, (window.devicePixelRatio || 1));
    const px = Math.round(cssSize * dpr);
    if (canvas.width !== px) { canvas.width = px; canvas.height = px; }
    canvas.style.width = "100%";
    canvas.style.height = "100%";
  }

  let last = 0;
  let frozen = false;      // 预览/测试钉帧：停掉 rAF 循环（pose 摆帧不受影响）
  function freeze() { frozen = true; }
  function frame(ts) {
    if (frozen) return;
    const now = (typeof ts === "number" && ts > 0) ? ts
      : (typeof performance !== "undefined" && performance.now
        ? performance.now() : 0);
    let dt = last ? (now - last) / 1000 : 0.016;
    last = now;
    if (!(dt > 0) || dt > 0.25) dt = 0.016;
    dt = Math.min(0.05, dt);
    update(dt);
    draw();
    requestAnimationFrame(frame);
  }

  /* ---------- 状态机 ---------- */
  function apply(state, text) {
    if (!wrapEl) return;
    if (!STATES.includes(state)) state = "idle";
    if (state !== cur) {
      STATES.forEach((s) => {
        wrapEl.classList.remove("s-" + s);
        container.classList.remove("s-" + s);
      });
      wrapEl.classList.add("s-" + state);
      container.classList.add("s-" + state);
      cur = state;
      if (state !== "speaking") { A.level = 0; }
    }
    const label = document.getElementById("stateText");
    if (label) {
      label.textContent = text || TEXT[state] || "";
    }
  }

  function level(v) {
    if (cur !== "speaking") { A.level = 0; return; }
    A.level = clamp(Number(v) || 0, 0, 1);
  }

  function setVoice(voiceId) {
    const raw = String(voiceId || "").trim();
    let next = LOOKS[VOICE_MAP[raw]];
    if (!next && raw.indexOf("local:") === 0)
      next = LOOKS[VOICE_MAP[raw.slice(6)]];
    if (!next) next = guessLook(raw);
    if (!next) return;
    look = next;
  }

  /* 离线摆一帧（无头截图 / 单元测试用）：不依赖 rAF。
   * force 可强制 {blink:0..1}（验证眨眼），仅供预览/测试使用 */
  function pose(state, lv, steps, force) {
    if (state) apply(state);
    if (typeof lv === "number") { if (state === "speaking") A.level = lv; }
    const n = steps || 1;
    for (let i = 0; i < n; i++) update(1 / 60);
    update(0);
    // 钉帧必须在 update(0) 之后：update 里即使 dt=0 也会按 blinkP 重算
    // A.blink，先钉会被覆盖
    if (force && typeof force.blink === "number") {
      A.blink = clamp(force.blink, 0, 1);
      A.blinking = force.blink > 0 && force.blink < 1;
    }
    draw();
  }

  /* ---------- 绘制 ---------- */
  const CX = 150, CY = 150, R = 106;     // 球心与半径（300 逻辑坐标）
  const INK = "#252b52";                 // 五官墨色（参考图的深藏青）

  function draw() {
    if (!ctx) return;
    const g = ctx;
    g.setTransform(dpr * cssSize / VP, 0, 0, dpr * cssSize / VP, 0, 0);
    g.clearRect(0, 0, VP, VP);

    g.save();
    // 整体运动：绕枢轴（球心稍下）微转 + 呼吸微缩放 + 漂浮平移
    g.translate(CX, CY + 22);
    g.rotate(A.headR * 0.35 * Math.PI / 180);
    const bs = 1 + A.breath * 0.012;
    g.scale(bs, bs);
    g.translate(-CX, -(CY + 22));
    g.translate(A.headX * 0.9, A.headY * 1.1);
    drawOrb(g);
    g.restore();
  }

  function drawOrb(g) {
    // 落影（随呼吸微胀缩，制造"悬浮球"感）
    g.save();
    g.globalAlpha = 0.30 + A.breath * 0.05;
    g.fillStyle = "#050a16";
    g.beginPath();
    g.ellipse(CX, CY + R + 16, R * 0.62, R * 0.11, 0, 0, Math.PI * 2);
    g.fill();
    g.restore();

    // 球体：对角渐变（本音色专属配色）
    const grad = g.createLinearGradient(CX - R, CY - R, CX + R, CY + R);
    grad.addColorStop(0, look.c1);
    grad.addColorStop(1, look.c2);
    g.fillStyle = grad;
    circle(g, CX, CY, R);
    g.fill();

    // 左上透光高光（弱一些，保住渐变的饱和度）
    const hi = g.createRadialGradient(
      CX - R * 0.38, CY - R * 0.46, R * 0.05,
      CX - R * 0.38, CY - R * 0.46, R * 1.05);
    hi.addColorStop(0, "rgba(255,255,255,.24)");
    hi.addColorStop(0.45, "rgba(255,255,255,.06)");
    hi.addColorStop(1, "rgba(255,255,255,0)");
    g.fillStyle = hi;
    circle(g, CX, CY, R);
    g.fill();

    // 边缘微光
    g.strokeStyle = "rgba(255,255,255,.10)";
    g.lineWidth = 1.5;
    circle(g, CX, CY, R - 0.8);
    g.stroke();

    // ---- 脸（全部裁进球内，杜绝任何"出界"）----
    g.save();
    circle(g, CX, CY, R - 1);
    g.clip();

    const gx = A.gazeX * 4.5, gy = A.gazeY * 3.5;
    const exo = 33;                       // 眼距中轴
    const eyy = CY - 10 + gy;             // 眼高（含视线垂直分量）

    // 眉毛：弧线，随状态抬/压
    const lift = -A.brow * 9;
    g.strokeStyle = INK;
    g.lineWidth = 5.5;
    g.lineCap = "round";
    for (const s of [-1, 1]) {
      const ex = CX + s * exo;
      g.beginPath();
      g.moveTo(ex - 13, eyy - 26 + lift);
      g.quadraticCurveTo(ex, eyy - 34 + lift, ex + 13, eyy - 26 + lift);
      g.stroke();
    }

    // 眼睛：圆点 + 高光；眨眼 = 垂直压扁（听时略睁大）
    const wide = cur === "listening" ? 1.08 : 1;
    const blinkS = Math.max(0.08, 1 - A.blink * 0.95) * wide;
    for (const s of [-1, 1]) {
      const ex = CX + s * exo + gx;
      g.save();
      g.translate(ex, eyy);
      g.scale(1, blinkS);
      g.fillStyle = INK;
      circle(g, 0, 0, 12.5);
      g.fill();
      g.fillStyle = "rgba(255,255,255,.92)";
      circle(g, -4, -4.5, 4.2);
      g.fill();
      g.restore();
    }
    // 闭眼缝线（压扁后补一条弧，闭眼更可读）
    if (A.blink > 0.55) {
      g.strokeStyle = INK;
      g.lineWidth = 3;
      g.lineCap = "round";
      for (const s of [-1, 1]) {
        const ex = CX + s * exo + gx;
        g.beginPath();
        g.moveTo(ex - 11, eyy + 1);
        g.quadraticCurveTo(ex, eyy + 4.5, ex + 11, eyy + 1);
        g.stroke();
      }
    }

    // 嘴：微笑弧线 ⇄ 开合椭圆（说话），圆/扁唇交替
    const my = CY + 26;
    const mh = A.open * 30;
    if (mh < 3) {
      g.strokeStyle = INK;
      g.lineWidth = 6;
      g.lineCap = "round";
      const w = 16 + A.smile * 5;
      g.beginPath();
      g.moveTo(CX - w, my - 3);
      g.quadraticCurveTo(CX, my + 6 + A.smile * 16, CX + w, my - 3);
      g.stroke();
    } else {
      const w = 15 * (0.9 + 0.2 * A.round - 0.12 * A.wide);
      g.fillStyle = INK;
      ell(g, CX, my + mh * 0.5, w, mh * 0.5 + 2.5, 0);
      g.fill();
      if (mh > 9) {                       // 舌头
        g.fillStyle = "rgba(255,122,144,.9)";
        ell(g, CX, my + mh * 0.74, w * 0.58, Math.max(2, mh * 0.2), 0);
        g.fill();
      }
    }

    g.restore();   // clip
  }

  /* ---------- 覆盖层：声波/思考点/搜索标记（样式见 style.css） ---------- */
  function el(tag, attrs, parent) {
    const n = document.createElementNS(NS, tag);
    if (attrs) for (const k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }

  function buildOverlay() {
    const svg = el("svg", { class: "av-over", viewBox: "0 0 260 260" });
    const waves = el("g", { class: "waves", fill: "none", stroke: "url(#waveG)",
      "stroke-linecap": "round" }, svg);
    const defs = el("defs", null, svg);
    const lg = el("linearGradient", { id: "waveG", x1: "0", y1: "0",
      x2: "1", y2: "0" }, defs);
    el("stop", { offset: "0%", "stop-color": "#5b93ff" }, lg);
    el("stop", { offset: "100%", "stop-color": "#8b5cf6" }, lg);
    [["M52 108 A34 34 0 0 0 52 152", "w1"],
     ["M34 96 A52 52 0 0 0 34 164", "w2"],
     ["M16 84 A70 70 0 0 0 16 176", "w3"],
     ["M208 108 A34 34 0 0 1 208 152", "w1"],
     ["M226 96 A52 52 0 0 1 226 164", "w2"],
     ["M244 84 A70 70 0 0 1 244 176", "w3"]].forEach(([d, cls]) =>
      el("path", { class: "wave " + cls, d: d }, waves));
    const think = el("g", { class: "think", fill: "#8fb6ff" }, svg);
    el("circle", { class: "tdot d1", cx: 104, cy: 46, r: 6 }, think);
    el("circle", { class: "tdot d2", cx: 130, cy: 38, r: 7 }, think);
    el("circle", { class: "tdot d3", cx: 156, cy: 46, r: 6 }, think);
    const sm = el("g", { class: "searchMark", fill: "none", stroke: "#5b93ff",
      "stroke-width": "4", "stroke-linecap": "round" }, svg);
    el("circle", { cx: 130, cy: 34, r: 9 }, sm);
    el("path", { d: "M137 41 L146 50" }, sm);
    container.parentNode.appendChild(svg);
  }

  function init() {
    container = document.getElementById("avatar");
    if (!container) return;
    wrapEl = container.parentNode;

    canvas = document.createElement("canvas");
    canvas.className = "av-canvas";
    container.appendChild(canvas);
    try {
      ctx = canvas.getContext("2d");
    } catch (e) {
      ctx = null;
    }
    resize();
    if (window.addEventListener) window.addEventListener("resize", resize);

    buildOverlay();
    // 先画一帧：无头截图 / 首屏都不会空白
    update(1 / 60);
    draw();
    requestAnimationFrame(frame);
  }

  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", init);
  else init();

  window.Avatar = {
    apply, level, setVoice, pose, freeze,
    get state() { return cur; },
    get char() { return look; },
    debug() {
      return {
        state: cur, open: A.open, blink: A.blink,
        round: A.round, wide: A.wide, gazeX: A.gazeX, gazeY: A.gazeY,
        headR: A.headR, breath: A.breath,
        smile: A.smile, brow: A.brow, name: look.name,
        colors: [look.c1, look.c2],
      };
    },
  };
})();
