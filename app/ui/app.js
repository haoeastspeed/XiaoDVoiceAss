/* 小D · 智能助手 —— 主界面逻辑
   主界面只有三个按钮；设置为抽屉；对话以漂浮气泡呈现。 */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const api = () => window.pywebview.api;

  const MAX_BUBBLES = 5;
  const SLOTS = [6, 23, 40, 57, 74];      // 气泡纵向槽位（百分比）

  let BOOT = null;
  let running = false;
  let polling = false;
  let started = false;
  let askAt = 0, ttftDone = true;
  let gwTick = 0;
  let gwActing = false;      // 网关正在启停时加密刷新，让文案立刻跟上

  /* ---------- 状态 ---------- */
  function setStatus(kind, text) {
    const dot = $("status").querySelector(".dot");
    dot.className = "dot" + (kind ? " " + kind : "");
    $("statusText").textContent = text;
  }
  function setAvatar(state, text) {
    if (window.Avatar) window.Avatar.apply(state, text);
  }
  const GW_ACTION = {
    starting: "正在启动网关",
    stopping: "正在停止网关",
    restarting: "正在重启网关",
  };
  function setGw(g) {
    if (!g) return;
    const acting = g.action ? (GW_ACTION[g.action] || "处理中…") : "";
    gwActing = !!acting;
    const el = $("gwPill");
    el.className = "pill " + (acting ? "busy" : (g.up ? "on" : "off"));
    el.textContent = acting ? ("网关 · " + acting)
                           : (g.up ? "网关 · 运行中 " : "网关 · 未运行 ") + g.port;
    el.title = (acting ? acting : (g.kind || "")) + (g.msg ? "\n" + g.msg : "") +
      (g.pid ? ("\nPID " + g.pid) : "");
    const b = $("gwBadge");
    if (b) {
      b.className = "gw-badge " + (acting ? "busy" : (g.up ? "on" : "off"));
      b.textContent = acting || (g.up ? "运行中" : "未运行");
    }
    const info = $("gwInfo");
    if (info) {
      info.textContent = (acting ? acting + " · " : (g.kind || "") + " · ") +
        (g.pid ? ("PID " + g.pid + " · ") : "") + (g.msg || "");
    }
    const mo = $("gwModels");
    if (mo) {
      // 模型状态 + 设备可达性（ESP32 连不上时这两行直接给出原因）
      const bits = [];
      if (g.models_msg) bits.push(g.models_msg);
      if (g.up && g.listen_addr) bits.push("监听 " + g.listen_addr +
        ":" + g.port);
      if (g.up && !g.lan_ok) {
        bits.push(g.lan_wanted
          ? "⚠ 仅本机可达，设备连不上 → 重启网关后再试"
          : "已设为仅本机（设备连不上）");
      }
      mo.textContent = bits.join(" · ");
      mo.className = "tip" + (g.up && g.lan_wanted && !g.lan_ok ? " warn"
        : (g.models_ready ? " ok" : ""));
    }
    setGwAddr(g);
  }

  function setGwAddr(g) {
    // 配网要的是完整 WebSocket 地址，只显示端口用户还得自己拼
    const lan = $("gwAddrLan"), loc = $("gwAddrLocal");
    if (lan) {
      lan.textContent = g.up
        ? (g.ws_lan || "未检测到局域网 IP")
        : "未运行（启动后显示）";
      lan.className = g.up && g.ws_lan ? "ok" : "muted";
    }
    if (loc) {
      loc.textContent = g.ws_local
        || ("ws://127.0.0.1:" + (g.port || 8000) + "/xiaozhi/v1/");
    }
  }
  function setButtons() {
    $("startBtn").disabled = running;
    $("stopBtn").disabled = !running;
    $("interruptBtn").disabled = !running;
  }
  function setLatency(ms) {
    const el = $("latPill");
    if (!ms) { el.hidden = true; return; }
    el.hidden = false;
    el.textContent = "首句 " + (ms / 1000).toFixed(1) + "s";
  }

  /* ---------- 对话气泡：只保留最新"一问一答" ---------- */
  let meBubble = null, botBubble = null, sysBubble = null, sysTimer = null;

  function mkBubble(cls, text) {
    const el = document.createElement("div");
    el.className = "bubble " + cls;
    const t = document.createElement("div");
    t.className = "txt";
    t.textContent = text || "";
    el.appendChild(t);
    $("bubbles").appendChild(el);
    return el;
  }
  function dropSys() {
    if (sysTimer) { clearTimeout(sysTimer); sysTimer = null; }
    if (sysBubble) { sysBubble.remove(); sysBubble = null; }
  }
  function showMe(text) {
    if (!text) return;
    if (meBubble) meBubble.remove();
    meBubble = mkBubble("me", text);
  }
  function startBot() {
    if (botBubble) botBubble.remove();
    botBubble = mkBubble("bot", "");
  }
  function appendBot(t) {
    if (!botBubble) startBot();
    const el = botBubble.querySelector(".txt");
    el.textContent += t;
    botBubble.scrollTop = botBubble.scrollHeight;
  }
  function showSys(text) {
    dropSys();
    sysBubble = mkBubble("sys", text);
    sysTimer = setTimeout(dropSys, 6000);
  }
  function showErr(text) {
    dropSys();
    sysBubble = mkBubble("err", text);
    sysTimer = setTimeout(dropSys, 9000);
  }
  function clearBubbles() {
    dropSys();
    if (meBubble) { meBubble.remove(); meBubble = null; }
    if (botBubble) { botBubble.remove(); botBubble = null; }
  }

  /* ---------- 事件 ---------- */
  function handle(evs) {
    for (const ev of evs) {
      const t = ev.type;
      if (t === "state") {
        if (ev.state === "starting" || ev.state === "connecting") {
          setStatus("busy", "正在准备…"); setAvatar("starting");
        } else if (ev.state === "listening") {
          setStatus("live", "正在聆听，请说话"); setAvatar("listening");
        }
      } else if (t === "ready") {
        setStatus("live", "正在聆听，请说话"); setAvatar("listening");
      } else if (t === "user") {
        if (ev.text) showMe(ev.text);
        askAt = performance.now(); ttftDone = false;
        setStatus("busy", "小D 思考中…"); setAvatar("thinking");
      } else if (t === "searching") {
        showSys("🔍 联网搜索：" + (ev.query || ""));
        setStatus("net", "正在联网搜索…"); setAvatar("searching");
      } else if (t === "search_done") {
        showSys(ev.count ? ("已找到 " + ev.count + " 条资料")
                                   : "没搜到结果，直接回答");
        setAvatar("thinking"); setStatus("busy", "小D 思考中…");
      } else if (t === "assistant_start") {
        setStatus("busy", "小D 回复中…"); setAvatar("speaking"); startBot();
      } else if (t === "assistant_delta") {
        appendBot(ev.text || "");
        if (!ttftDone && askAt) { ttftDone = true; setLatency(performance.now() - askAt); }
      } else if (t === "assistant_stop") {
        setStatus("live", "正在聆听，请说话"); setAvatar("listening");
      } else if (t === "muted") {
        setStatus("busy", "小D 正在说…（Esc 可打断）"); setAvatar("speaking");
      } else if (t === "unmuted") {
        setStatus("live", "正在聆听，请说话"); setAvatar("listening");
      } else if (t === "level") {
        const v = Math.max(0, Math.min(1, Number(ev.value) || 0));
        const halo = $("halo");
        if (halo) halo.style.setProperty("--halo", (1 + v * 0.30).toFixed(3));
        // 口型只跟播放电平走（播放期麦克风回调仍在跑，不能拿来动嘴）
        if (ev.src === "play" && window.Avatar && window.Avatar.level)
          window.Avatar.level(v);
      } else if (t === "reconnecting") {
        setStatus("busy", "连接中断，正在重连…（第 " + (ev.attempt || 1) + " 次）");
        setAvatar("starting");
      } else if (t === "closed") {
        setStatus("err", "连接已断开"); setAvatar("off");
      } else if (t === "notice") {
        /* 内部提示（音频端点回退等）没有 text，只写日志不弹气泡；
           带 text 的是要告诉用户的（TTS 降级/恢复等） */
        if (ev.text) showSys(ev.text);
      } else if (t === "mic_warning") {
        showErr(ev.error || "麦克风没有采集到声音");
        setStatus("err", "麦克风无音"); setAvatar("off");
      } else if (t === "error") {
        showErr(ev.error || "出错了");
        setStatus("err", "出错了"); setAvatar("off");
      }
    }
  }

  async function refreshGw() {
    try {
      const g = await api().gateway_status();
      if (g && g.ok) setGw(g);
    } catch (e) { /* ignore */ }
  }

  async function poll() {
    if (polling) return;
    polling = true;
    try {
      const r = await api().poll();
      if (r.events && r.events.length) handle(r.events);
      if (gwActing || ++gwTick % 12 === 0) refreshGw();   // 空闲约 3s 一次；操作中每 250ms
      if (typeof r.running === "boolean" && r.running !== running) {
        running = r.running;
        setButtons();
        if (!running) {
          setStatus("", "已结束聊天"); setAvatar("idle"); setLatency(0);
        }
      }
    } catch (e) { /* 桥未就绪 */ }
    polling = false;
    setTimeout(poll, 250);
  }

  /* ---------- 下拉（支持分组）---------- */
  function fillSel(sel, items, cur, role) {
    sel.innerHTML = "";
    const groups = {};
    let hasGroup = false;
    for (const it of items) {
      if (it.group) {
        hasGroup = true;
        if (!groups[it.group]) {
          groups[it.group] = document.createElement("optgroup");
          groups[it.group].label = it.group;
          sel.appendChild(groups[it.group]);
        }
        const o = document.createElement("option");
        o.value = it.id;
        o.textContent = it.name;
        groups[it.group].appendChild(o);
      } else {
        const o = document.createElement("option");
        o.value = it.id !== undefined ? it.id : it.index;
        o.textContent = it.name;
        sel.appendChild(o);
      }
    }
    if (cur != null && [...sel.options].some((o) => String(o.value) === String(cur)))
      sel.value = cur;
    else if ((role === "mic" || role === "spk") && items.length) {
      const arr = items.find((d) => role === "mic" && d.name.includes("阵列")
        && !d.bluetooth);
      sel.value = (arr || items[0]).index;
    }
    return hasGroup;
  }

  /* ---------- 初始化 ---------- */
  async function bootstrap() {
    let r;
    try { r = await api().bootstrap(); } catch (e) { return; }
    if (!r || !r.ok) return;
    BOOT = r;
    const dev = r.devices || {};
    if (dev.error) showErr(dev.error);
    fillSel($("micSel"), dev.inputs || [], r.cfg.mic_index, "mic");
    fillSel($("spkSel"), dev.outputs || [], r.cfg.speaker_index, "spk");
    fillSel($("modelSel"), r.models || [], r.cfg.model);
    fillSel($("voiceSel"), r.voices || [], r.cfg.voice);
    fillSel($("modeSel"), r.interrupt_modes || [], r.cfg.interrupt_mode);
    fillSel($("presetSel"), (r.presets || []).map((p) => ({ id: p.name, name: p.name })),
      r.cfg.prompt_name);
    $("promptBox").value = r.cfg.prompt || "";
    if (window.Avatar && window.Avatar.setVoice)
      window.Avatar.setVoice(r.cfg.voice || "");
    const vol = Number(r.cfg.play_volume) || 1.0;
    $("volRange").value = String(vol);
    $("volVal").textContent = Math.round(vol * 100) + "%";
    $("topChk").checked = !!r.cfg.always_on_top;
    $("gwAutoChk").checked = r.cfg.auto_start_gateway !== false;
    $("autoChk").checked = !!r.cfg.auto_start_call;
    $("saveChk").checked = !!r.cfg.save_transcript;
    $("bootChk").checked = !!r.autostart;
    $("searchChk").checked = !!r.cfg.web_search;
    $("searchMaxSel").value = String(r.cfg.web_search_max || 4);
    if ($("gwHostSel")) {
      $("gwHostSel").value = String(r.cfg.gateway_host || "0.0.0.0") ===
        "127.0.0.1" ? "127.0.0.1" : "0.0.0.0";
    }
    if ($("echoCancelSel")) {
      $("echoCancelSel").value = String(r.cfg.echo_cancel || "auto") === "off"
        ? "off" : "auto";
    }
    if ($("echoCoolSel")) {
      // 配置值不一定正好落在档位上：就近吸附，避免下拉框显示为空
      const cool = Number(r.cfg.echo_gate_cooldown_ms || 700);
      const opts = [300, 500, 700, 1000, 1500];
      let best = opts[0];
      for (const o of opts) { if (Math.abs(o - cool) < Math.abs(best - cool)) best = o; }
      $("echoCoolSel").value = String(best);
    }
    applyLlm(r);
    fillSel($("memorySel"), r.memory_modes || [], r.cfg.memory_mode || "full");
    // 压缩用小模型：复用本地模型列表，前面加一个"同对话模型"
    if ($("memModelSel")) {
      const mm = [{ id: "", name: "与对话模型相同" }].concat(r.models || []);
      fillSel($("memModelSel"), mm, r.cfg.memory_summary_model || "");
    }
    fillSel($("langSel"), r.asr_languages || [], r.cfg.asr_language || "auto");
    if ($("sleepSel")) {
      const sv = String(r.cfg.idle_sleep_s || 0);
      const sopts = ["0", "120", "300", "600", "1800"];
      $("sleepSel").value = sopts.includes(sv) ? sv : "300";
    }
    $("haChk").checked = !!r.cfg.ha_enabled;
    $("haBaseInp").value = r.cfg.ha_base_url || "";
    $("haTokenInp").value = r.cfg.ha_token || "";
    $("haBox").hidden = !r.cfg.ha_enabled;
    // ---- 工具与扩展（MCP / 视觉 / 声纹 / 实时 / 唤醒词）----
    $("toolsFirstChk").checked = !!r.cfg.tools_first;
    $("mcpChk").checked = r.cfg.mcp_enabled !== false;
    $("visionChk").checked = !!r.cfg.vision_enabled;
    $("camIdxInp").value = (r.cfg.vision_camera_index ?? 0);
    $("vpChk").checked = !!r.cfg.voiceprint_enabled;
    $("vpBaseInp").value = r.cfg.voiceprint_base_url || "";
    $("rtChk").checked = !!r.cfg.realtime_enabled;
    $("rtBaseInp").value = r.cfg.realtime_base_url || "";
    $("wakeSel").value = r.cfg.wake_mode || "always";
    $("dataDirTip").textContent = "记录目录：" + r.data_dir + "\\transcripts";
    $("aboutTip").textContent = "小D · 智能助手 v" + r.version +
      "　离线识别 + 本地大模型；语音合成支持在线神经音色与系统本地离线音色双引擎。";
    setGw(r.gateway);
    running = !!r.running;
    setButtons();
    if (running) { setStatus("live", "正在聆听，请说话"); setAvatar("listening"); }
    else { setStatus("", "未开始聊天"); setAvatar("idle"); }
    if (!started) { started = true; poll(); }
  }

  /* ---------- 语言模型后端 ---------- */
  function applyLlm(r) {
    const backend = r.cfg.llm_backend || "ollama";
    fillSel($("llmBackendSel"), r.llm_backends || [], backend);
    const presets = [{ id: "", name: "自定义" }].concat(
      (r.llm_presets || []).map((p) => ({ id: p.id, name: p.name })));
    fillSel($("llmPresetSel"), presets, r.cfg.llm_preset || "");
    $("llmBaseInp").value = r.cfg.llm_api_base || "";
    $("llmKeyInp").value = r.cfg.llm_api_key || "";
    $("llmOnlineBox").hidden = backend !== "openai";
    applyKeyHint(r);
  }

  function applyKeyHint(r) {
    const pid = $("llmPresetSel") ? $("llmPresetSel").value : "";
    const p = (r.llm_presets || []).find((x) => x.id === pid);
    $("llmKeyTip").textContent = p
      ? "「" + p.name + "」的 Key 形如：" + (p.key_hint || "")
      : "";
  }

  // 语种 → 推荐音色（切换语言时给用户一键套用，不强制改）
  const LANG_VOICE = {
    auto: "", zh: "zh-CN-XiaoxiaoNeural", yue: "zh-HK-HiuGaaiNeural",
    en: "en-US-JennyNeural", ja: "ja-JP-NanamiNeural", ko: "ko-KR-SunHiNeural",
  };

  /* ---------- 抽屉 ---------- */
  function openDrawer() { $("drawer").hidden = false; $("drawerMask").hidden = false; }
  function closeDrawer() { $("drawer").hidden = true; $("drawerMask").hidden = true; }
  $("settingsBtn").addEventListener("click", openDrawer);
  $("drawerClose").addEventListener("click", closeDrawer);
  $("drawerMask").addEventListener("click", closeDrawer);

  /* ---------- 三个主按钮 ---------- */
  $("startBtn").addEventListener("click", async () => {
    setStatus("busy", "正在准备…"); setAvatar("starting");
    const r = await api().start_call(parseInt($("micSel").value, 10),
      parseInt($("spkSel").value, 10));
    if (r && !r.ok) {
      showErr(r.error || "启动失败");
      setStatus("err", "启动失败"); setAvatar("off");
    } else { running = true; setButtons(); }
  });
  $("stopBtn").addEventListener("click", async () => {
    await api().stop_call();
    running = false; setButtons(); setLatency(0);
    setStatus("", "已结束聊天"); setAvatar("idle");
  });
  $("interruptBtn").addEventListener("click", () => {
    api().interrupt(); setAvatar("listening");
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") {
      ev.preventDefault();
      if (!$("drawer").hidden) { closeDrawer(); return; }
      api().interrupt();
    }
  });

  /* ---------- 双击主界面空白处：全屏 / 还原 ---------- */
  $("stage").addEventListener("dblclick", (ev) => {
    // 气泡和各类控件上不触发，避免误切
    if (ev.target.closest("button, select, textarea, input, a, label, .bubble"))
      return;
    api().toggle_fullscreen().then((r) => {
      if (r && r.ok)
        showSys(r.fullscreen ? "已全屏，双击空白处还原" : "已退出全屏");
      else if (r)
        showErr("切换全屏失败：" + (r.error || ""));
    }).catch(() => {});
  });

  /* ---------- 设置项 ---------- */
  $("micSel").addEventListener("change", async () => {
    await api().set_option("mic_index", parseInt($("micSel").value, 10));
    await api().set_option("mic_name",
      $("micSel").selectedOptions[0] ? $("micSel").selectedOptions[0].textContent : "");
    showSys("麦克风已切换，下次聊天生效");
  });
  $("spkSel").addEventListener("change", async () => {
    await api().set_option("speaker_index", parseInt($("spkSel").value, 10));
    await api().set_option("speaker_name",
      $("spkSel").selectedOptions[0] ? $("spkSel").selectedOptions[0].textContent : "");
    showSys("扬声器已切换，下次聊天生效");
  });
  $("rescanBtn").addEventListener("click", async () => {
    const r = await api().rescan_devices();
    fillSel($("micSel"), r.devices.inputs || [], parseInt($("micSel").value, 10), "mic");
    fillSel($("spkSel"), r.devices.outputs || [], parseInt($("spkSel").value, 10), "spk");
    showSys("设备已重新扫描");
  });
  $("modelSel").addEventListener("change", async () => {
    if ($("modelSel").value === "__no_ollama__") {
      showErr("未检测到模型服务：请先启动 Ollama，并确保已下载模型");
      return;
    }
    const r = await api().set_model($("modelSel").value);
    if (r && r.ok)
      showSys("模型已切换为 " + $("modelSel").value + "（下一句生效）");
  });
  $("voiceSel").addEventListener("change", async () => {
    const v = $("voiceSel").value;
    const r = await api().set_voice(v);
    if (window.Avatar && window.Avatar.setVoice)
      window.Avatar.setVoice(v);
    showSys("音色已切换（下一句生效）" + (r && r.label ? " · " + r.label : ""));
  });
  $("previewBtn").addEventListener("click", async () => {
    const el = $("previewTip");
    el.textContent = "正在合成…";
    const r = await api().preview_voice();
    if (r && r.ok) {
      // 报出实际用的设备：用户第一反应总是"没声音"，而多半是放到了别的设备上
      el.textContent = "已播放试听（" + (r.engine === "local" ? "本地合成"
        : "在线合成") + (r.voice ? " · " + r.voice : "")
        + (r.device ? " → " + r.device : "") + "）";
    } else {
      el.textContent = "试听失败：" + ((r && r.error) || "");
    }
    setTimeout(() => { el.textContent = ""; }, 6000);
  });
  $("modeSel").addEventListener("change", async () => {
    const mode = $("modeSel").value;
    await api().set_interrupt(mode);
    showSys(mode === "voice"
      ? "语音打断已开启：播放中连续说约 0.5 秒即打断（戴耳机或靠近麦克风更灵敏）"
      : "打断方式已切换为按钮 / Esc");
  });
  $("searchChk").addEventListener("change", async () => {
    const r = await api().set_search($("searchChk").checked);
    if (r && !r.ok) {
      $("searchChk").checked = !$("searchChk").checked;
      showErr("设置失败：" + (r.error || ""));
    } else {
      showSys($("searchChk").checked
        ? "已开启联网搜索：需要查实时信息时小D会自己去搜" : "已关闭联网搜索");
    }
  });
  $("searchMaxSel").addEventListener("change", async () => {
    await api().set_option("web_search_max", parseInt($("searchMaxSel").value, 10));
  });
  $("presetSel").addEventListener("change", async () => {
    const name = $("presetSel").value;
    const r = await api().set_prompt(name, "");
    if (r && r.ok) {
      const b = await api().bootstrap();
      BOOT = b;
      $("promptBox").value = b.cfg.prompt || "";
      // 预设自带推荐音色：后端已经切了，这里同步下拉和虚拟形象
      fillSel($("voiceSel"), b.voices || [], b.cfg.voice);
      if (window.Avatar && window.Avatar.setVoice)
        window.Avatar.setVoice(b.cfg.voice || "");
      showSys(r.voice
        ? ("已套用「" + name + "」，音色同步为推荐音色")
        : ("已套用预设人设：" + name));
    }
  });
  $("promptApply").addEventListener("click", async () => {
    const r = await api().set_prompt($("presetSel").value, $("promptBox").value);
    if (r && r.ok) showSys("人设已更新（下一轮生效）");
    else showErr((r && r.error) || "保存人设失败");
  });
  $("volRange").addEventListener("change", async () => {
    const v = Number($("volRange").value) || 1.0;
    $("volVal").textContent = Math.round(v * 100) + "%";
    const r = await api().set_option("play_volume", v);
    if (r && r.ok) showSys("播放音量已设为 " + Math.round(v * 100) + "%");
  });
  $("volRange").addEventListener("input", () => {
    $("volVal").textContent = Math.round((Number($("volRange").value) || 1) * 100) + "%";
  });
  $("topChk").addEventListener("change", () => api().set_option("always_on_top", $("topChk").checked));
  $("gwAutoChk").addEventListener("change", () => {
    api().set_option("auto_start_gateway", $("gwAutoChk").checked);
    showSys($("gwAutoChk").checked
      ? "已开启：打开软件自动启动网关并加载离线模型"
      : "已关闭：需要手动点「启动网关」");
  });
  /* ---------- 语言模型后端 ---------- */
  $("llmBackendSel").addEventListener("change", async () => {
    const v = $("llmBackendSel").value;
    $("llmOnlineBox").hidden = v !== "openai";
    await api().set_option("llm_backend", v);
    showSys(v === "openai"
      ? "已切到在线 API：填好地址和 Key 后点「测试连通性」（正在重启网关）"
      : "已切回本地 Ollama，离线可用（正在重启网关）");
  });
  $("llmPresetSel").addEventListener("change", async () => {
    const pid = $("llmPresetSel").value;
    const p = (BOOT.llm_presets || []).find((x) => x.id === pid);
    if (!p) { applyKeyHint(BOOT); return; }
    $("llmBaseInp").value = p.base_url || "";
    await api().set_option("llm_preset", pid);
    await api().set_option("llm_api_base", p.base_url || "");
    if (p.model) {
      $("modelSel").value = p.model;
      await api().set_option("model", p.model);
    }
    applyKeyHint(BOOT);
    showSys("已套用「" + p.name + "」，填好 Key 后点「测试连通性」");
  });
  $("llmBaseInp").addEventListener("change", () =>
    api().set_option("llm_api_base", $("llmBaseInp").value.trim()));
  $("llmKeyInp").addEventListener("change", () =>
    api().set_option("llm_api_key", $("llmKeyInp").value.trim()));
  $("llmTestBtn").addEventListener("click", async () => {
    $("llmTestTip").textContent = "测试中…";
    const r = await api().test_llm();
    $("llmTestTip").textContent = (r && r.msg) || "测试失败";
    if (r && r.ok) showSys(r.msg); else showErr((r && r.msg) || "连接失败");
  });

  /* ---------- 记忆 / 语言 / 休眠 / 家居 ---------- */
  $("memorySel").addEventListener("change", async () => {
    const v = $("memorySel").value;
    await api().set_option("memory_mode", v);
    showSys(v === "none" ? "已切为无记忆：每句话都独立"
      : v === "summary" ? "已切为本地总结：较早的对话会被压成摘要"
      : "已切为超长记忆：保留完整对话");
  });
  $("memModelSel").addEventListener("change", () =>
    api().set_option("memory_summary_model", $("memModelSel").value));
  $("langSel").addEventListener("change", async () => {
    const v = $("langSel").value;
    await api().set_option("asr_language", v);
    const rec = LANG_VOICE[v];
    $("langTip").textContent = rec ? "推荐音色：" + rec : "自动判断语种";
  });
  $("langVoiceBtn").addEventListener("click", async () => {
    const rec = LANG_VOICE[$("langSel").value];
    if (!rec) { showSys("「自动判断」没有对应的推荐音色"); return; }
    await api().set_option("voice", rec);
    const b = await api().bootstrap();
    BOOT = b;
    fillSel($("voiceSel"), b.voices || [], b.cfg.voice);
    if (window.Avatar && window.Avatar.setVoice)
      window.Avatar.setVoice(b.cfg.voice || "");
    showSys("音色已切换为 " + rec);
  });
  $("sleepSel").addEventListener("change", () =>
    api().set_option("idle_sleep_s", parseInt($("sleepSel").value, 10)));
  $("haChk").addEventListener("change", () => {
    const on = $("haChk").checked;
    $("haBox").hidden = !on;
    api().set_option("ha_enabled", on);
    showSys(on ? "已开启智能家居，填好地址和令牌后点「测试连接」"
               : "已关闭智能家居");
  });
  $("haBaseInp").addEventListener("change", () =>
    api().set_option("ha_base_url", $("haBaseInp").value.trim()));
  $("haTokenInp").addEventListener("change", () =>
    api().set_option("ha_token", $("haTokenInp").value.trim()));
  $("haTestBtn").addEventListener("click", async () => {
    $("haTestTip").textContent = "测试中…";
    const r = await api().ha_test();
    $("haTestTip").textContent = (r && r.msg) || "测试失败";
    if (r && r.ok) showSys(r.msg); else showErr((r && r.msg) || "连接失败");
  });

  $("gwHostSel").addEventListener("change", async () => {
    const v = $("gwHostSel").value;
    showSys(v === "127.0.0.1"
      ? "已改为仅本机：ESP32 等设备将连不上（正在重启网关）"
      : "已改为局域网可连，设备配网填下方「本机地址」（正在重启网关）");
    await api().set_option("gateway_host", v);
  });
  if ($("echoCancelSel")) {
    $("echoCancelSel").addEventListener("change", async () => {
      const v = $("echoCancelSel").value;
      showSys(v === "off"
        ? "回声消除已关闭：播放期不判打断，只认按钮 / Esc（正在重启网关）"
        : "回声消除已开启：消掉自己的回声后仍可语音打断（正在重启网关）");
      await api().set_option("echo_cancel", v);
    });
  }
  if ($("echoCoolSel")) {
    $("echoCoolSel").addEventListener("change", async () => {
      const v = Number($("echoCoolSel").value);
      showSys("说完后静默 " + (v / 1000).toFixed(1) + " 秒再开始听（正在重启网关）");
      await api().set_option("echo_gate_cooldown_ms", v);
    });
  }
  $("autoChk").addEventListener("change", () => api().set_option("auto_start_call", $("autoChk").checked));
  // ---- 工具与扩展（MCP / 视觉 / 声纹 / 实时 / 唤醒词）----
  $("toolsFirstChk").addEventListener("change", async () => {
    await api().set_option("tools_first", $("toolsFirstChk").checked);
    showSys("已" + ($("toolsFirstChk").checked ? "开启" : "关闭") + "工具调用，重启网关后生效");
  });
  $("mcpChk").addEventListener("change", async () => {
    await api().set_option("mcp_enabled", $("mcpChk").checked);
    showSys("已" + ($("mcpChk").checked ? "启用" : "关闭") + " MCP，重启网关后生效");
  });
  $("visionChk").addEventListener("change", () =>
    api().set_option("vision_enabled", $("visionChk").checked));
  $("camIdxInp").addEventListener("change", () =>
    api().set_option("vision_camera_index", parseInt($("camIdxInp").value, 10) || 0));
  $("vpChk").addEventListener("change", () =>
    api().set_option("voiceprint_enabled", $("vpChk").checked));
  $("vpBaseInp").addEventListener("change", () =>
    api().set_option("voiceprint_base_url", $("vpBaseInp").value.trim()));
  $("rtChk").addEventListener("change", () =>
    api().set_option("realtime_enabled", $("rtChk").checked));
  $("rtBaseInp").addEventListener("change", () =>
    api().set_option("realtime_base_url", $("rtBaseInp").value.trim()));
  $("wakeSel").addEventListener("change", async () => {
    await api().set_option("wake_mode", $("wakeSel").value);
    showSys("唤醒方式已更新，下次开始聊天生效");
  });
  $("saveChk").addEventListener("change", () => {
    api().set_option("save_transcript", $("saveChk").checked);
    showSys($("saveChk").checked ? "结束后自动保存对话记录" : "已关闭自动保存");
  });
  $("bootChk").addEventListener("change", async () => {
    const r = await api().set_autostart($("bootChk").checked);
    if (!r || !r.ok) {
      $("bootChk").checked = !$("bootChk").checked;
      showErr("设置开机自启失败" + ((r && r.error) ? "：" + r.error : ""));
    } else {
      showSys($("bootChk").checked ? "已设为开机自动启动" : "已取消开机自启");
    }
  });
  function bindGw(btnId, fn, tip) {
    $(btnId).addEventListener("click", async () => {
      const r = await fn();
      if (r && !r.ok) showErr((r.error || "操作失败"));
      else showSys(tip);
      setTimeout(refreshGw, 800);
      setTimeout(refreshGw, 3000);
      setTimeout(refreshGw, 8000);
    });
  }
  bindGw("gwStartBtn", () => api().gateway_start(), "正在启动语音网关…");
  bindGw("gwStopBtn", () => api().gateway_stop(), "网关已停止");
  bindGw("gwRestartBtn", () => api().gateway_restart(), "正在重启语音网关…");
  $("gwLogBtn").addEventListener("click", async () => {
    const r = await api().open_gateway_log();
    if (r && !r.ok) showErr("打开日志失败：" + (r.error || ""));
  });
  $("gwCopyBtn").addEventListener("click", async () => {
    const txt = ($("gwAddrLan").textContent || "").trim();
    if (!txt || txt.indexOf("ws://") !== 0) {
      showErr("网关还没运行，暂无可复制的地址");
      return;
    }
    let ok = false;
    try {
      await navigator.clipboard.writeText(txt);
      ok = true;
    } catch (e) {
      try {
        const r = await api().copy_text(txt);
        ok = !!(r && r.ok);
      } catch (e2) { ok = false; }
    }
    if (ok) showSys("已复制网关地址：" + txt);
    else showErr("复制失败，请手动选中地址按 Ctrl+C");
  });

  $("openRecBtn").addEventListener("click", async () => {
    const r = await api().open_transcripts();
    if (r && !r.ok) showErr("打开记录目录失败：" + (r.error || ""));
  });
  $("exportBtn").addEventListener("click", async () => {
    const r = await api().export_transcript();
    if (r && r.ok) showSys("已导出：" + r.path);
    else showErr((r && r.error) || "导出失败");
  });
  $("clearBtn").addEventListener("click", async () => {
    await api().clear_history();
    clearBubbles();
    showSys("对话已清空");
  });

  window.addEventListener("pywebviewready", bootstrap);
  if (window.pywebview && window.pywebview.api) setTimeout(bootstrap, 60);
})();
