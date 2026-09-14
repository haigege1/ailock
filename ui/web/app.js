/* ============================================================
   AiLock · Web UI 逻辑
   桥接：运行在 pywebview 中时调用 window.pywebview.api.*；
        浏览器预览时回退到 MockApi（纯前端演示）。
   ============================================================ */
(function () {
  "use strict";

  /* -------------------- 桥接层 -------------------- */
  var API = null;
  var apiReady = false;
  function markReady() {
    if (window.pywebview && window.pywebview.api) {
      API = window.pywebview.api;
      apiReady = true;
    }
  }
  markReady();
  window.addEventListener("pywebviewready", function () { markReady(); });

  function call(method) {
    var args = Array.prototype.slice.call(arguments, 1);
    return new Promise(function (resolve) {
      if (apiReady && API && typeof API[method] === "function") {
        Promise.resolve(API[method].apply(API, args))
          .then(resolve).catch(function () { resolve(null); });
      } else if (typeof MockApi[method] === "function") {
        Promise.resolve(MockApi[method].apply(MockApi, args)).then(resolve);
      } else {
        resolve(null);
      }
    });
  }

  /* 浏览器预览用 Mock */
  var MockApi = {
    getConfig: function () {
      return Promise.resolve({
        background: { mode: "image", dim: 0.45, blur: 0, kenburns: true },
        ui: { animations: true },
        display: { keep_on: true },
        lock: { min_lock_minutes: 0, auto_lock_idle_minutes: 0, lock_on_start: false },
        message: { text: "AI 正在跑，请勿关机" },
        status: { enabled: true, on_done_action: "shutdown", on_done_countdown: 90, poll_seconds: 2, path: "%APPDATA%\\AiLock\\status.json" },
        security: { max_attempts: 5, cooldown_seconds: 30 },
        autostart: false,
        hotkey: "ctrl+alt+l"
      });
    },
    setConfig: function (k, v) { console.log("setConfig", k, v); return Promise.resolve(true); },
    unlock: function (pw) {
      if (pw === "1234") return Promise.resolve({ ok: true, msg: "" });
      return Promise.resolve({ ok: false, msg: "密码错误，还可尝试 4 次" });
    },
    changePassword: function (oldP, newP) {
      if (oldP !== "1234") return Promise.resolve({ ok: false, msg: "旧密码不正确" });
      if (newP.length < 4) return Promise.resolve({ ok: false, msg: "新密码至少 4 位" });
      return Promise.resolve({ ok: true, msg: "已保存" });
    },
    getWallpapers: function () {
      var g = [
        "linear-gradient(160deg,#cfe9ff 0%,#e8f3ff 45%,#b9d8c2 100%)",
        "linear-gradient(160deg,#04342C 0%,#085041 60%,#0F6E56 100%)",
        "linear-gradient(160deg,#2b1a4a 0%,#6d3b6e 52%,#c98a6b 100%)",
        "linear-gradient(160deg,#0b1e3a 0%,#1b4d6b 50%,#2ea3a3 100%)"
      ];
      var n = ["晨雾", "深林", "暮光", "极光"];
      var list = g.map(function (x, i) { return { name: n[i], url: x }; });
      return Promise.resolve({ list: list, index: 0, ken: true });
    },
    switchWall: function (dir) {
      return Promise.resolve({ index: (Math.random() * 4) | 0, ken: true });
    },
    isPasswordSet: function () { return Promise.resolve(true); },
    lockNow: function () { return Promise.resolve(true); },
    closeSettings: function () { return Promise.resolve(true); }
  };

  /* -------------------- 工具 -------------------- */
  function $(id) { return document.getElementById(id); }
  function pad(n) { return String(n).padStart(2, "0"); }

  /* ============================================================
     锁屏视图
     ============================================================ */
  var Lock = (function () {
    var bg, clock, date, msg, card, pw, errMsg, toast, unlocked, pending, navL, navR;
    var walls = [], wi = 0, ken = true, toastTimer = null, unlockedTimer = null;

    function init() {
      bg = $("bg"); clock = $("clock"); date = $("date"); msg = $("msg");
      card = $("card"); pw = $("pw"); errMsg = $("errMsg"); toast = $("toast");
      unlocked = $("unlocked"); pending = $("pending");
      navL = $("arrowL"); navR = $("arrowR");

      tick(); setInterval(tick, 1000);
      resetEntrance();

      navL.onclick = function () { switchWall(-1); };
      navR.onclick = function () { switchWall(1); };
      $("unlock").onclick = tryUnlock;
      pw.addEventListener("keydown", function (e) { if (e.key === "Enter") tryUnlock(); });

      document.addEventListener("keydown", function (e) {
        if (e.key === "ArrowLeft") switchWall(-1);
        else if (e.key === "ArrowRight") switchWall(1);
      });

      // 加载壁纸
      call("getWallpapers").then(function (r) {
        if (r && r.list) {
          walls = r.list; wi = r.index || 0; ken = !!r.ken;
          applyWall(wi, false);
        }
      });

      // 加载外观配置：暗化遮罩 / 背景模糊 / 锁屏提示语
      call("getConfig").then(function (c) {
        if (!c) return;
        var b = c.background || {};
        applyBackdrop(b.dim, b.blur);
        if (c.message && c.message.text) window.setMessage(c.message.text);
      });
    }

    function tick() {
      var d = new Date();
      clock.textContent = pad(d.getHours()) + ":" + pad(d.getMinutes());
      var wk = ["星期日", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六"][d.getDay()];
      date.textContent = d.getFullYear() + "年" + (d.getMonth() + 1) + "月" + d.getDate() + "日 " + wk;
    }

    function resetEntrance() {
      if (!enableAnim()) return;
      [clock, date, card].forEach(function (el) {
        el.classList.remove("in"); void el.offsetWidth; el.classList.add("in");
      });
    }
    function enableAnim() {
      // 由设置开关控制；默认开
      return Lock._anim !== false;
    }

    function applyWall(i, withToast) {
      if (!walls.length) return;
      var w = walls[i];
      bg.style.backgroundImage = w.url;
      bg.classList.toggle("ken", ken && enableAnim());
      if (withToast) {
        toast.textContent = "壁纸 " + (i + 1) + " / " + walls.length + " · " + w.name;
        toast.classList.add("show");
        clearTimeout(toastTimer);
        toastTimer = setTimeout(function () { toast.classList.remove("show"); }, 2200);
      }
    }

    // 应用暗化遮罩（veil 透明度）与背景模糊（bg filter），均来自设置
    function applyBackdrop(dim, blur) {
      if (!bg) return;
      var v = $("veil");
      if (v) v.style.opacity = (dim == null ? 0.45 : dim);
      bg.style.filter = (blur && blur > 0) ? ("blur(" + blur + "px)") : "none";
    }

    function switchWall(dir) {
      if (!walls.length) return;
      wi = (wi + dir + walls.length) % walls.length;
      applyWall(wi, true);
      call("switchWall", dir).then(function (r) {
        if (r && typeof r.index === "number") {
          wi = r.index; ken = !!r.ken; applyWall(wi, false);
        }
      });
    }

    function tryUnlock() {
      var val = pw.value || "";
      call("unlock", val).then(function (r) {
        if (r && r.ok) {
          // 成功：后端会销毁窗口；这里播放淡出
          unlocked.classList.add("show");
          setTimeout(function () { pw.value = ""; }, 800);
        } else {
          doErr(r && r.msg ? r.msg : "密码错误");
        }
      });
    }
    function doErr(text) {
      card.classList.remove("shake"); void card.offsetWidth; card.classList.add("shake");
      pw.classList.add("err");
      errMsg.textContent = text;
      errMsg.classList.add("show");
      setTimeout(function () { pw.classList.remove("err"); errMsg.classList.remove("show"); }, 4000);
    }

    /* 后端推送接口（全局，供 evaluate_js 调用） */
    window.updateStatus = function (data) {
      if (!data) return;
      var bar = $("statusBar");
      var running = (data.state || "").toLowerCase() === "running";
      var dot = $("statusDot"), taskEl = $("statusTask");
      dot.classList.toggle("off", !running);
      $("statusItem").classList.toggle("off", !running);
      var label = running ? "AI 任务运行中" : "空闲";
      if (data.task) label = (running ? "AI · " : "") + data.task;
      taskEl.textContent = label;
      if (data.min_lock_left && data.min_lock_left > 0) {
        $("statusLock").style.display = "";
        $("statusLockVal").textContent = mmss(data.min_lock_left);
      } else {
        $("statusLock").style.display = "none";
      }
    };
    window.showPending = function (obj) {
      if (!obj) { pending.classList.remove("show"); return; }
      $("paTitle").textContent = (obj.reason || "任务完成");
      $("paCount").textContent = (obj.remaining || 0) + " 秒";
      pending.classList.add("show");
      if (obj._tick) return;
      // 简易倒计时刷新
      if (pending._timer) clearInterval(pending._timer);
      pending._timer = setInterval(function () {
        var left = obj.remaining - 1; obj.remaining = Math.max(0, left);
        $("paCount").textContent = obj.remaining + " 秒";
        if (obj.remaining <= 0) { clearInterval(pending._timer); }
      }, 1000);
    };
    window.setMessage = function (text, color) {
      msg.textContent = text || "";
      msg.style.color = color || "rgba(255,255,255,.82)";
    };
    window.applyWallpaper = function (url) {
      if (url) { walls = [{ name: "", url: url }]; wi = 0; applyWall(0, false); }
    };
    window.setAnim = function (on) { Lock._anim = !!on; bg.classList.toggle("ken", ken && on); };

    function mmss(s) {
      s = Math.max(0, Math.round(s));
      return pad((s / 60) | 0) + ":" + pad(s % 60);
    }

    return { init: init };
  })();

  /* ============================================================
     设置视图
     ============================================================ */
  var Settings = (function () {
    var cfg = {};
    var navItems, panels;

    function init() {
      navItems = document.querySelectorAll(".nav-item");
      panels = document.querySelectorAll(".panel");
      navItems.forEach(function (n) {
        n.onclick = function () {
          navItems.forEach(function (x) { x.classList.toggle("active", x === n); });
          panels.forEach(function (p) { p.classList.toggle("active", p.dataset.sec === n.dataset.sec); });
        };
      });
      $("winClose").onclick = function () { call("closeSettings"); };

      call("getConfig").then(function (c) {
        if (c) { cfg = c; populate(); }
        else console.warn("getConfig 返回空");
      });
    }

    function val(path, def) {
      var node = cfg; var parts = path.split(".");
      for (var i = 0; i < parts.length; i++) {
        if (node == null || typeof node !== "object") return def;
        node = node[parts[i]];
      }
      return node === undefined ? def : node;
    }

    function setSwitch(sel, key, transform) {
      var el = $(sel); if (!el) return;
      el.checked = !!val(key);
      el.onchange = function () {
        var v = el.checked;
        if (transform) v = transform(v);
        call("setConfig", key, v);
      };
    }
    function setSelect(sel, key, map) {
      var el = $(sel); if (!el) return;
      var v = val(key);
      // map: 反查 option value
      for (var i = 0; i < el.options.length; i++) {
        if (map ? map.toStore(el.options[i].value) == v : el.options[i].value == String(v)) {
          el.selectedIndex = i; break;
        }
      }
      el.onchange = function () {
        var v2 = el.value;
        if (map) v2 = map.toStore(v2);
        call("setConfig", key, v2);
      };
    }
    function setText(sel, key) {
      var el = $(sel); if (!el) return;
      el.value = val(key, "");
      el.onchange = el.oninput = function () { call("setConfig", key, el.value); };
    }
    function setSeg(sel, key) {
      var box = $(sel); if (!box) return;
      var v = String(val(key));
      box.querySelectorAll("button").forEach(function (b) {
        b.classList.toggle("on", b.dataset.val === v);
        b.onclick = function () {
          box.querySelectorAll("button").forEach(function (x) { x.classList.toggle("on", x === b); });
          call("setConfig", key, b.dataset.val);
        };
      });
    }
    function setSlider(sel, valSel, key, scale, fmt) {
      var el = $(sel), out = $(valSel); if (!el) return;
      el.value = Math.round((val(key, 0) * (scale || 1)));
      if (out) out.textContent = fmt ? fmt(el.value) : el.value;
      el.oninput = function () {
        if (out) out.textContent = fmt ? fmt(el.value) : el.value;
        call("setConfig", key, parseFloat(el.value) / (scale || 1));
      };
    }

    function populate() {
      // 常规
      setSwitch("swStartLock", "lock.lock_on_start");
      setSelect("selIdle", "lock.auto_lock_idle_minutes", {
        toStore: function (v) { return v === "off" ? 0 : parseInt(v, 10); }
      });
      setSelect("selMinLock", "lock.min_lock_minutes", {
        toStore: function (v) { return v === "off" ? 0 : parseInt(v, 10); }
      });
      setSwitch("swKeepOn", "display.keep_on");
      setText("txtMsg", "message.text");

      // 安全
      setSwitch("swAnim", "ui.animations", function (v) {
        call("setAnim", v); return v;
      });
      $("secPolicy").textContent = val("security.max_attempts", 5) + " 次 / "
        + val("security.cooldown_seconds", 30) + " 秒";
      bindPassword();

      // 壁纸
      setSeg("segMode", "background.mode");
      setSlider("slDim", "slDimVal", "background.dim", 100, function (v) { return v + "%"; });
      setSlider("slBlur", "slBlurVal", "background.blur", 1, function (v) { return v + " px"; });
      setSwitch("swKen", "background.kenburns");

      // AI 联动
      setSwitch("swStatus", "status.enabled");
      setSelect("selAction", "status.on_done_action");
      setText("txtCountdown", "status.on_done_countdown");
      setText("txtStatusPath", "status.path");
      setText("txtPoll", "status.poll_seconds");
    }

    function bindPassword() {
      var oldP = $("pwOld"), newP = $("pwNew"), cfm = $("pwCfm");
      var btn = $("pwSave"), fb = $("pwFeedback");
      btn.onclick = function () {
        fb.className = "pw-feedback"; fb.textContent = "";
        var o = oldP.value, n = newP.value, c = cfm.value;
        if (n.length < 4) { fb.className = "pw-feedback err"; fb.textContent = "新密码至少 4 位"; return; }
        if (n !== c) { fb.className = "pw-feedback err"; fb.textContent = "两次输入的新密码不一致"; return; }
        btn.disabled = true;
        call("changePassword", o, n).then(function (r) {
          btn.disabled = false;
          if (r && r.ok) {
            fb.className = "pw-feedback ok"; fb.textContent = "已保存 ✓";
            oldP.value = ""; newP.value = ""; cfm.value = "";
            btn.textContent = "已保存 ✓"; btn.classList.add("ok");
            setTimeout(function () { btn.textContent = "保存密码"; btn.classList.remove("ok"); }, 1500);
          } else {
            fb.className = "pw-feedback err"; fb.textContent = (r && r.msg) || "保存失败";
          }
        });
      };
    }

    return { init: init };
  })();

  /* ============================================================
     启动
     ============================================================ */
  function showOnly(view) {
    $("lock").style.display = (view === "lock") ? "flex" : "none";
    $("settings").style.display = (view === "settings") ? "flex" : "none";
    $("lock").classList.toggle("show", view === "lock");
    $("settings").classList.toggle("show", view === "settings");
  }

  function boot() {
    // App 模式（data: URI 内嵌）：window.__VIEW__ / window.__APP_MODE__
    // 浏览器预览模式：URL ?view=lock/settings
    var view = (typeof window.__VIEW__ !== "undefined") ? window.__VIEW__ : null;
    var appMode = !!window.__APP_MODE__;
    if (!view) {
      var params = new URLSearchParams(location.search);
      view = params.get("view");
    }
    var preview = !view;
    if (view === "lock") { showOnly("lock"); Lock.init(); }
    else if (view === "settings") { showOnly("settings"); Settings.init(); }
    else if (appMode) {
      // data: URI 模式下无 view 标志：默认锁屏
      showOnly("lock"); Lock.init();
    }
    else {
      $("previewBar").classList.add("show");
      // 预览：两个视图都建，用顶部条切换
      showOnly("lock"); Lock.init();
      // 预览模式下 settings 也初始化（隐藏），切到设置时显示
      var tabs = document.querySelectorAll("#previewBar .tab");
      tabs.forEach(function (t) {
        t.onclick = function () {
          tabs.forEach(function (x) { x.classList.toggle("active", x === t); });
          var v = t.dataset.view;
          if (v === "settings" && !Settings._inited) { Settings._inited = true; Settings.init(); }
          showOnly(v);
        };
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else { boot(); }
})();
