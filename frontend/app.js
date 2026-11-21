/* ============================================================================
 * 个人量化工作台 · 看板前端（本地/云端共用同一份实现）
 *
 * - 纯原生 JS：无构建、无依赖、无第三方图表库；图表全部手写 SVG / DOM。
 * - 模式由 index.html 的 window.QT_MODE 决定：
 *     local —— 60s 轮询 /api/signals + /api/paper + /api/equity + /api/backtest
 *     cloud —— GET /api/signals 首屏 + GET /api/stream (SSE)，断线指数退避 1s→8s
 * - 安全：数据请求遇到 401 + {"challenge":true} 时弹出 Turnstile 人机验证，
 *     验证 token POST /api/verify 换 HttpOnly cookie 后重试；纯静态/本地无网关时
 *     降级为"开发模式"直接请求数据。429 显示限流倒计时。
 * - 动效（docs/DESIGN.md §2）：数字 rAF 补间 480ms、列表 FLIP 240ms、
 *     净值描边生长 700ms、骨架屏、prefers-reduced-motion 全关、只动 transform/opacity。
 * ========================================================================== */
(function () {
  "use strict";

  /* ============================== 常量 ============================== */
  var MODE = window.QT_MODE === "cloud" ? "cloud" : "local";
  var IS_CLOUD = MODE === "cloud";

  var EP = {
    config: "/api/config",
    signals: "/api/signals",
    paper: "/api/paper",
    equity: "/api/equity",
    backtest: "/api/backtest",
    verify: "/api/verify",
    stream: "/api/stream",
    market: "/api/market"
  };

  /* 安全必需白名单：Turnstile 官方组件。除此之外页面不加载任何外部资源。 */
  var TURNSTILE_SRC = "https://challenges.cloudflare.com/turnstile/v0/api.js";

  var POLL_MS = 60000;
  var TWEEN_MS = 480;        // DESIGN §2：数字补间 400–600ms
  var FLIP_MS = 240;         // DESIGN §2：列表 FLIP 240ms
  var DRAW_MS = 700;         // DESIGN §2：净值描边生长 600–800ms
  var MAX_SIGNAL_ROWS = 14;
  var HTTP_TIMEOUT = 9000;

  var SYMBOL_NAMES = {
    "600519": "贵州茅台",
    "000001": "平安银行",
    "300750": "宁德时代"
  };

  var DIR_LABEL = { up: "看涨", flat: "震荡", down: "看跌" };
  var BENCH_LABEL = {
    always_flat: "全走平 always_flat",
    majority: "多数类 majority",
    ma5_cross: "MA5 交叉 ma5_cross",
    random: "随机 random"
  };

  /* ============================== 小工具 ============================== */
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function num(v) {
    if (typeof v === "number" && Number.isFinite(v)) return v;
    if (typeof v === "string" && v.trim() !== "") {
      var n = Number(v.replace(/[,%\s]/g, ""));
      if (Number.isFinite(n)) return n;
    }
    return undefined;
  }

  function pick(obj) {
    for (var i = 1; i < arguments.length; i += 1) {
      var key = arguments[i];
      if (obj && obj[key] !== undefined && obj[key] !== null && obj[key] !== "") return obj[key];
    }
    return undefined;
  }

  function path(obj, dotted) {
    var node = obj;
    var parts = dotted.split(".");
    for (var i = 0; i < parts.length; i += 1) {
      if (node == null || typeof node !== "object") return undefined;
      node = node[parts[i]];
    }
    return node;
  }

  function pickPath(obj) {
    for (var i = 1; i < arguments.length; i += 1) {
      var v = path(obj, arguments[i]);
      if (v !== undefined && v !== null && v !== "") return v;
    }
    return undefined;
  }

  function arr(v) { return Array.isArray(v) ? v : []; }
  function obj(v) { return v && typeof v === "object" && !Array.isArray(v) ? v : {}; }

  var motionQuery = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
  function reduced() { return Boolean(motionQuery && motionQuery.matches); }

  function wait(ms) {
    return reduced() ? Promise.resolve() : sleep(ms);
  }

  /* cubic-bezier 求值，让 JS 补间与 CSS 使用同一条曲线（DESIGN §2） */
  function cubicBezier(x1, y1, x2, y2) {
    var cx = 3 * x1, bx = 3 * (x2 - x1) - cx, ax = 1 - cx - bx;
    var cy = 3 * y1, by = 3 * (y2 - y1) - cy, ay = 1 - cy - by;
    function sampleX(t) { return ((ax * t + bx) * t + cx) * t; }
    function sampleY(t) { return ((ay * t + by) * t + cy) * t; }
    function sampleDX(t) { return (3 * ax * t + 2 * bx) * t + cx; }
    return function (x) {
      var t = clamp(x, 0, 1);
      for (var i = 0; i < 5; i += 1) {
        var dx = sampleDX(t);
        if (Math.abs(dx) < 1e-6) break;
        t -= (sampleX(t) - x) / dx;
      }
      return sampleY(clamp(t, 0, 1));
    };
  }
  var easeState = cubicBezier(0.22, 1, 0.36, 1);

  /* ---- 数字格式化（全部 tabular-nums，非数字显示破折号） ---- */
  var DASH = "\u2014";
  function isNum(v) { return typeof v === "number" && Number.isFinite(v); }
  function fmtInt(v) { return isNum(v) ? Math.round(v).toLocaleString("en-US") : DASH; }
  function fmtMoney(v, digits) {
    if (!isNum(v)) return DASH;
    var d = digits == null ? 2 : digits;
    var sign = v < 0 ? "-" : "";
    return sign + "\u00A5" + Math.abs(v).toLocaleString("en-US", {
      minimumFractionDigits: d, maximumFractionDigits: d
    });
  }
  function fmtMoney0(v) { return fmtMoney(v, 0); }
  function fmtPct(v, digits) {
    if (!isNum(v)) return DASH;
    return (v * 100).toFixed(digits == null ? 1 : digits) + "%";
  }
  function fmtSignedPct(v, digits) {
    if (!isNum(v)) return DASH;
    var d = digits == null ? 2 : digits;
    return (v > 0 ? "+" : "") + (v * 100).toFixed(d) + "%";
  }
  function fmtSignedMoney(v) {
    if (!isNum(v)) return DASH;
    return (v > 0 ? "+" : v < 0 ? "-" : "") + "\u00A5" +
      Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function fmtPrice(v) {
    if (!isNum(v)) return DASH;
    return v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function fmtFixed(v, digits) { return isNum(v) ? v.toFixed(digits) : DASH; }
  function fmtFeature(v) {
    if (!isNum(v)) return DASH;
    var a = Math.abs(v);
    if (a >= 100) return v.toFixed(1);
    if (a >= 1) return v.toFixed(2);
    return v.toFixed(4);
  }
  function fmtPP(v) {
    if (!isNum(v)) return DASH;
    return (v > 0 ? "+" : "") + v.toFixed(1) + "pp";
  }

  /* ---- 时间（统一按 Asia/Shanghai 展示） ---- */
  var TZ_FMT = null;
  function shParts(value) {
    var d = value instanceof Date ? value : new Date(value);
    if (!Number.isFinite(d.getTime())) return null;
    try {
      TZ_FMT = TZ_FMT || new Intl.DateTimeFormat("en-GB", {
        timeZone: "Asia/Shanghai", hour12: false, weekday: "short",
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit"
      });
      var out = {};
      TZ_FMT.formatToParts(d).forEach(function (p) { if (p.type !== "literal") out[p.type] = p.value; });
      return {
        y: out.year, m: out.month, d: out.day,
        hh: out.hour === "24" ? "00" : out.hour, mm: out.minute, ss: out.second,
        wd: out.weekday
      };
    } catch (e) {
      return {
        y: String(d.getFullYear()), m: String(d.getMonth() + 1).padStart(2, "0"),
        d: String(d.getDate()).padStart(2, "0"),
        hh: String(d.getHours()).padStart(2, "0"),
        mm: String(d.getMinutes()).padStart(2, "0"),
        ss: String(d.getSeconds()).padStart(2, "0"),
        wd: "Mon"
      };
    }
  }
  function fmtHM(value) {
    var p = shParts(value);
    return p ? p.hh + ":" + p.mm : DASH;
  }
  function fmtHMS(value) {
    var p = shParts(value);
    return p ? p.hh + ":" + p.mm + ":" + p.ss : DASH;
  }
  function fmtMDHM(value) {
    var p = shParts(value);
    return p ? p.m + "-" + p.d + " " + p.hh + ":" + p.mm : DASH;
  }
  function fmtFullTime(value) {
    var p = shParts(value);
    return p ? p.y + "-" + p.m + "-" + p.d + " " + p.hh + ":" + p.mm + ":" + p.ss : DASH;
  }
  function fmtDate(value) {
    var p = shParts(value);
    return p ? p.y + "-" + p.m + "-" + p.d : DASH;
  }
  function fmtDateShort(value) {
    var p = shParts(value);
    return p ? p.m + "-" + p.d : DASH;
  }
  function rawDate(value) {
    var s = String(value == null ? "" : value);
    var m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
    return m ? m[1] + "-" + m[2] + "-" + m[3] : "";
  }

  /* ---- 数字 rAF 补间（旧值→新值，避免跳变） ---- */
  var tweenRaf = new WeakMap();
  function cancelTween(el) {
    var raf = tweenRaf.get(el);
    if (raf) { cancelAnimationFrame(raf); tweenRaf.delete(el); }
  }
  function tweenNumber(el, target, format, duration) {
    if (!el) return;
    var fmt = format || function (v) { return String(Math.round(v)); };
    if (!isNum(target)) {
      cancelTween(el); el.__num = undefined; el.__cur = undefined;
      if (el.textContent !== fmt(target)) el.textContent = DASH;
      return;
    }    var from = isNum(el.__cur) ? el.__cur : target;
    var prevTarget = el.__num;
    el.__num = target;
    if (reduced() || prevTarget === target || Math.abs(target - from) < 1e-9 || duration === 0) {
      cancelTween(el); el.__cur = target;
      var txt = fmt(target);
      if (el.textContent !== txt) el.textContent = txt;
      return;
    }
    cancelTween(el);
    var t0 = performance.now();
    var dur = duration == null ? TWEEN_MS : duration;
    function tick(now) {
      var p = clamp((now - t0) / dur, 0, 1);
      var v = from + (target - from) * easeState(p);
      el.__cur = v;
      var next = fmt(v);
      if (el.textContent !== next) el.textContent = next;
      if (p < 1) tweenRaf.set(el, requestAnimationFrame(tick));
      else { el.__cur = target; el.textContent = fmt(target); tweenRaf.delete(el); }
    }
    tweenRaf.set(el, requestAnimationFrame(tick));
  }
  function setText(el, text) { if (el && el.textContent !== text) el.textContent = text; }
  function show(el, visible) { if (el) el.hidden = !visible; }
  function tone(el, value) {
    if (!el) return;
    el.classList.toggle("is-pos", isNum(value) && value > 0);
    el.classList.toggle("is-neg", isNum(value) && value < 0);
  }
  function animate(el, keyframes, options) {
    if (!el || reduced() || typeof el.animate !== "function") return null;
    try { return el.animate(keyframes, options); } catch (e) { return null; }
  }

  /* ============================== 状态 ============================== */
  var state = {
    mode: MODE,
    signals: [],
    paper: null,
    equity: [],
    backtest: null,
    backtestError: null,
    selected: "",
    conn: "connecting",
    lastOkAt: 0,
    lastAsof: "",
    gate: {
      required: false,
      blocked: false,
      passed: false,
      active: false,
      sitekey: "",
      widgetId: null,
      scriptPromise: null,
      configPromise: null,
      verifying: false,
      configured: false
    },
    dev: false,
    lastAnnouncedId: "",
    rateLimitUntil: 0,
    rateLimitTimer: null,
    pollTimer: null,
    inflight: false,
    refreshQueued: false,
    hidden: false,
    stream: null,
    streamBackoff: 1000,
    streamTimer: null,
    streamPaused: false,
    rows: new Map(),
    eqGeo: null,
    rendered: { sigTable: "", paper: "", orders: "", backtest: "", equity: "" }
  };

  var B = {};
  var L = {};
  function collectDom() {
    $$("[data-bind]").forEach(function (el) { B[el.dataset.bind] = el; });
    $$("[data-list]").forEach(function (el) { L[el.dataset.list] = el; });
  }

  /* ============================== 请求层 ============================== */
  function parseRetryAfter(headerValue, data) {
    var sec = parseFloat(String(headerValue || "").trim());
    if (Number.isFinite(sec)) return clamp(sec, 1, 600);
    var body = num(data && (data.retry_after || data.retryAfter || data.cooldown_sec));
    if (Number.isFinite(body)) return clamp(body, 1, 600);
    return 30;
  }

  function request(pathValue, options) {
    var opts = options || {};
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, opts.timeoutMs || HTTP_TIMEOUT);
    var init = {
      method: opts.method || "GET",
      headers: Object.assign({ accept: "application/json" }, opts.headers || {}),
      credentials: "same-origin",
      cache: "no-store",
      signal: ctrl.signal
    };
    if (opts.body !== undefined) init.body = opts.body;
    return fetch(pathValue, init).then(function (res) {
      clearTimeout(timer);
      var ct = res.headers.get("content-type") || "";
      if (ct.indexOf("application/json") === -1) return { res: res, data: null };
      return res.json().then(function (data) { return { res: res, data: data }; }, function () {
        return { res: res, data: null };
      });
    }).catch(function (err) {
      clearTimeout(timer);
      throw err;
    });
  }

  function isChallengeBody(data) {
    return Boolean(data && (data.challenge === true || data.challenge_required === true ||
      data.error === "challenge_required"));
  }

  function getJSON(pathValue, options) {
    return request(pathValue, options).then(function (r) {
      var status = r.res.status;
      if ((status === 401 || status === 403) && isChallengeBody(r.data)) {
        return { challenge: true, status: status, data: r.data };
      }
      if (status === 429) {
        return { rateLimited: true, status: status, retryAfter: parseRetryAfter(r.res.headers.get("retry-after"), r.data) };
      }
      if (!r.res.ok) {
        var message = r.data && (r.data.message || r.data.error) ? (r.data.message || r.data.error) : "HTTP " + status;
        return { error: { message: message, status: status, data: r.data } };
      }
      return { data: r.data };
    }, function (err) {
      return { error: { message: (err && err.name === "AbortError") ? "请求超时" : "网络不可用", status: 0 } };
    });
  }

  /* ============================== 人机验证（Turnstile） ============================== */
  function pickSitekey(cfg) {
    return String(pickPath(cfg, "turnstile_sitekey", "turnstileSitekey", "sitekey",
      "turnstile.sitekey", "turnstile.siteKey", "turnstile_site_key") || "");
  }

  function setDevMode(on, reason) {
    state.dev = Boolean(on);
    var tag = $("#devTag");
    if (tag) {
      tag.hidden = !on;
      tag.title = on ? String(reason || "未检测到人机验证网关，直接读取数据") : "";
    }
    if (B["foot.gate"]) setText(B["foot.gate"], on ? "开发模式" : "人机验证");
  }

  function detectGate() {
    if (state.gate.configPromise) return state.gate.configPromise;
    var req = function () {
      return getJSON(EP.config, { timeoutMs: 4000 }).then(function (r) {
        if (r.error || r.rateLimited || r.challenge) {
          setDevMode(true, IS_CLOUD ? "验证配置不可用（GET /api/config）" : "本地开发模式：未启用访问验证");
          return;
        }
        applyGateConfig(r.data);
      });
    };
    state.gate.configPromise = req().then(function (value) {
      state.gate.configPromise = null;
      return value;
    }, function (err) {
      state.gate.configPromise = null;
      setDevMode(true, "验证配置不可用（GET /api/config）");
    });
    return state.gate.configPromise;
  }

  function applyGateConfig(cfg) {
    var sitekey = pickSitekey(cfg);
    var challenge = cfg && (cfg.challenge === true || cfg.challenge_required === true || sitekey !== "");
    state.gate.configured = true;
    state.gate.sitekey = sitekey;
    if (challenge && sitekey) {
      state.gate.required = true;
      setDevMode(false);
    } else {
      state.gate.required = false;
      setDevMode(true, challenge ? "验证配置缺失 sitekey" : "访问验证未启用");
    }
  }

  function loadTurnstile() {
    if (window.turnstile && typeof window.turnstile.render === "function") return Promise.resolve(window.turnstile);
    if (state.gate.scriptPromise) return state.gate.scriptPromise;
    state.gate.scriptPromise = new Promise(function (resolve, reject) {
      var settled = false;
      var timer = setTimeout(function () {
        if (settled) return;
        settled = true; reject(new Error("turnstile timeout"));
      }, 9000);
      var script = document.createElement("script");
      script.src = TURNSTILE_SRC;
      script.async = true;
      script.defer = true;
      script.onload = function () {
        if (settled) return;
        settled = true; clearTimeout(timer);
        if (window.turnstile && typeof window.turnstile.render === "function") resolve(window.turnstile);
        else reject(new Error("turnstile unavailable"));
      };
      script.onerror = function () {
        if (settled) return;
        settled = true; clearTimeout(timer);
        reject(new Error("turnstile load failed"));
      };
      document.head.appendChild(script);
    });
    return state.gate.scriptPromise;
  }

  function setGateStatus(text) { setText($("#gateStatus"), text); }
  function gateFail(message) {
    var el = $("#gateFail");
    if (el) { el.textContent = message; el.hidden = false; }
    var card = $("#gateCard");
    if (card) {
      card.classList.add("is-error");
      animate(card, [
        { transform: "translateX(0)" },
        { transform: "translateX(-3px)" },
        { transform: "translateX(3px)" },
        { transform: "translateX(-2px)" },
        { transform: "translateX(0)" }
      ], { duration: 240, iterations: 1, easing: "ease-in-out" });
      setTimeout(function () { card.classList.remove("is-error"); }, 600);
    }
  }

  function resetGateWidget() {
    if (state.gate.widgetId !== null && window.turnstile && window.turnstile.reset) {
      try { window.turnstile.reset(state.gate.widgetId); } catch (e) { /* ignore */ }
    }
  }

  function renderGateWidget() {
    var ts = window.turnstile;
    var box = $("#gateWidget");
    if (!ts || !box) { gateFail("验证组件不可用，请刷新页面重试"); return; }
    if (state.gate.widgetId !== null) {
      resetGateWidget();
      state.gate.widgetId = null;
    }
    box.innerHTML = "";
    $("#gateCheck").hidden = true;
    box.hidden = false;
    state.gate.widgetId = ts.render(box, {
      sitekey: state.gate.sitekey,
      theme: "dark",
      language: "zh-cn",
      appearance: "always",
      retry: "never",
      callback: function (token) { verifyToken(token); },
      "expired-callback": function () { setGateStatus("验证已过期，请重新完成验证"); },
      "error-callback": function () {
        gateFail("验证组件出错，请点击重新验证");
      }
    });
    setGateStatus("请完成下方人机验证");
    var card = $("#gateCard");
    if (card && card.focus) card.focus();
  }

  function openGate() {
    if (state.gate.active) return Promise.resolve();
    state.gate.active = true;
    stopPolling();
    pauseStream();
    var gate = $("#gate");
    if (!gate) return Promise.resolve();
    gate.hidden = false;
    document.body.classList.add("gate-open");
    var card = $("#gateCard");
    if (card) { card.classList.remove("is-success"); card.classList.remove("is-error"); }
    $("#gateCheck").hidden = true;
    $("#gateWidget").hidden = false;
    $("#gateWidget").innerHTML = "";
    var fail = $("#gateFail");
    if (fail) { fail.hidden = true; fail.textContent = ""; }
    setGateStatus("正在加载验证组件…");
    requestAnimationFrame(function () {
      gate.classList.add("is-open");
      if (card && card.focus) card.focus();
    });

    var ready = state.gate.sitekey
      ? Promise.resolve()
      : detectGate().then(function () {
          if (!state.gate.sitekey) throw new Error("missing sitekey");
        });

    return ready.then(loadTurnstile).then(function () {
      renderGateWidget();
    }).catch(function () {
      state.gate.scriptPromise = null;
      setGateStatus("验证组件加载失败");
      gateFail("无法加载 Turnstile 组件（需要联网）。检查网络后点击「重新验证」。");
    });
  }

  function closeGate() {
    var gate = $("#gate");
    state.gate.active = false;
    document.body.classList.remove("gate-open");
    if (!gate) return;
    if (reduced()) { gate.hidden = true; gate.classList.remove("is-open"); return; }
    gate.classList.remove("is-open");
    setTimeout(function () { if (!state.gate.active) gate.hidden = true; }, 210);
  }

  function gateSuccess() {
    state.gate.passed = true;
    state.gate.blocked = false;
    state.gate.required = true;
    $("#gateWidget").hidden = true;
    var check = $("#gateCheck");
    if (check) {
      check.hidden = false;
      var svgPath = check.querySelector("path");
      if (svgPath && svgPath.getTotalLength) {
        var len = svgPath.getTotalLength();
        svgPath.style.strokeDasharray = len + "px";
        svgPath.style.strokeDashoffset = reduced() ? "0px" : len + "px";
        if (!reduced() && svgPath.animate) {
          svgPath.animate(
            [{ strokeDashoffset: len + "px" }, { strokeDashoffset: "0px" }],
            { duration: 240, easing: "ease-out", fill: "forwards" }
          );
        }
      }
      setGateStatus("验证通过 · 正在载入数据");
      $("#gateCard").classList.add("is-success");
    }
    return wait(420).then(function () {
      closeGate();
      void refreshAll("after-gate");
      if (IS_CLOUD) { state.streamBackoff = 1000; state.streamPaused = false; startStream(); }
      else startPolling();
    });
  }

  function verifyToken(token) {
    if (state.gate.verifying) return;
    state.gate.verifying = true;
    setGateStatus("正在校验验证结果…");
    request(EP.verify, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ token: token }),
      timeoutMs: 12000
    }).then(function (r) {
      state.gate.verifying = false;
      if (r.res.ok) {
        setGateStatus("验证通过");
        return gateSuccess();
      }
      if (r.res.status === 429) {
        var sec = parseRetryAfter(r.res.headers.get("retry-after"), r.data);
        setGateStatus("验证请求过于频繁，稍后重试");
        gateFail("请求过于频繁，" + sec + " 秒后重试");
        return;
      }
      setGateStatus("验证未通过");
      gateFail("验证未通过，请重新完成验证");
      resetGateWidget();
    }).catch(function () {
      state.gate.verifying = false;
      setGateStatus("验证请求失败");
      gateFail("验证请求失败（网络不可用），请重试");
      resetGateWidget();
    });
  }

  function retryGate() {
    var fail = $("#gateFail");
    if (fail) { fail.hidden = true; fail.textContent = ""; }
    $("#gateCheck").hidden = true;
    if (window.turnstile && state.gate.widgetId !== null) {
      $("#gateWidget").hidden = false;
      resetGateWidget();
      setGateStatus("请完成下方人机验证");
      return;
    }
    state.gate.scriptPromise = null;
    void openGateAgain();
  }

  function openGateAgain() {
    state.gate.active = false;
    $("#gate").classList.remove("is-open");
    $("#gate").hidden = true;
    return openGate();
  }

  /* ============================== 限流（429） ============================== */
  function startRateLimit(seconds) {
    var sec = clamp(Number.isFinite(seconds) ? seconds : 30, 5, 600);
    var until = Date.now() + sec * 1000;
    if (until <= state.rateLimitUntil) return;
    state.rateLimitUntil = until;
    var notice = $("#notice");
    if (notice) notice.hidden = false;
    stopPolling();
    if (state.rateLimitTimer) clearInterval(state.rateLimitTimer);
    state.rateLimitTimer = setInterval(tickRateLimit, 1000);
    tickRateLimit();
  }

  function tickRateLimit() {
    var left = Math.ceil((state.rateLimitUntil - Date.now()) / 1000);
    var count = $("#noticeCount");
    if (left <= 0) {
      if (state.rateLimitTimer) clearInterval(state.rateLimitTimer);
      state.rateLimitTimer = null;
      state.rateLimitUntil = 0;
      var notice = $("#notice");
      if (notice) notice.hidden = true;
      if (state.gate.active) return;
      startPolling();
      void refreshAll("rate-limit-end");
      return;
    }
    if (count) count.textContent = left + "s";
  }

  function clearRateLimit() {
    if (state.rateLimitTimer) clearInterval(state.rateLimitTimer);
    state.rateLimitTimer = null;
    state.rateLimitUntil = 0;
    var notice = $("#notice");
    if (notice) notice.hidden = true;
    if (!state.gate.active) { startPolling(); void refreshAll("manual"); }
  }

  /* ============================== 数据归一化 ============================== */
  function nameOf(symbol) {
    return SYMBOL_NAMES[symbol] || "";
  }

  function asRatio(v) {
    var n = num(v);
    if (!Number.isFinite(n)) return undefined;
    /* 本项目的回测 JSON 用小数（0.42）；兼容少数把百分数（42.0）直接吐出来的实现 */
    return Math.abs(n) > 1.5 ? n / 100 : n;
  }

  function normCI(raw) {
    var v = raw;
    if (typeof v === "string") {
      var m = v.match(/[-\d.]+/g);
      if (!m || m.length < 2) return null;
      v = [parseFloat(m[0]), parseFloat(m[1])];
    }
    if (Array.isArray(v) && v.length >= 2) {
      var lo = num(v[0]), hi = num(v[1]);
      if (!Number.isFinite(lo) || !Number.isFinite(hi)) return null;
      return { low: lo, high: hi };
    }
    if (v && typeof v === "object") {
      var a = num(pick(v, "low", "lower", "lo", "min", "left"));
      var b = num(pick(v, "high", "upper", "hi", "max", "right"));
      if (Number.isFinite(a) && Number.isFinite(b)) return { low: a, high: b };
    }
    return null;
  }

  function normFeatureList(raw) {
    var list = raw;
    if (typeof list === "string") {
      try { list = JSON.parse(list); } catch (e) { list = []; }
    }
    return arr(list).map(function (f) {
      if (!f || typeof f !== "object") return null;
      var name = pick(f, "name", "feature", "key");
      if (!name) return null;
      return {
        name: String(name),
        value: num(pick(f, "value", "val", "x")),
        contrib: num(pick(f, "contrib", "contribution", "weight")) || 0
      };
    }).filter(Boolean).sort(function (a, b) {
      return Math.abs(b.contrib) - Math.abs(a.contrib);
    }).slice(0, 5);
  }

  function normProb(raw, fallback) {
    var src = obj(raw);
    var up = num(pick(src, "up", "prob_up", "UP"));
    var flat = num(pick(src, "flat", "prob_flat", "FLAT"));
    var down = num(pick(src, "down", "prob_down", "DOWN"));
    if (![up, flat, down].every(Number.isFinite) && fallback) {
      up = num(fallback.up); flat = num(fallback.flat); down = num(fallback.down);
    }
    if (![up, flat, down].every(Number.isFinite)) return null;
    var sum = up + flat + down;
    if (sum > 0 && Math.abs(sum - 1) > 1e-6) { up /= sum; flat /= sum; down /= sum; }
    return { up: up, flat: flat, down: down };
  }

  function normSignal(raw) {
    if (!raw || typeof raw !== "object") return null;
    var symbol = String(pick(raw, "symbol", "code") || "").trim();
    if (!symbol) return null;
    var prob = normProb(pick(raw, "prob", "probabilities", "probability"));
    if (!prob) {
      prob = { up: 0, flat: 0, down: 0 };
    }
    var dirRaw = String(pick(raw, "direction", "dir") || "flat").toLowerCase();
    var direction = dirRaw === "up" || dirRaw === "down" ? dirRaw : "flat";
    var statusRaw = String(pick(raw, "status") || "ok").toLowerCase();
    var status = statusRaw === "stale" || statusRaw === "warming_up" ? statusRaw : "ok";
    var asof = pick(raw, "asof", "created_at", "trade_date") || "";
    var confidence = num(pick(raw, "confidence"));
    if (!Number.isFinite(confidence)) confidence = Math.max(prob.up, prob.flat, prob.down);
    return {
      id: String(pick(raw, "signal_id", "id") || (symbol + "-" + asof)),
      symbol: symbol,
      name: String(pick(raw, "name", "stock_name", "display_name") || nameOf(symbol)),
      asof: String(asof),
      tradeDate: String(pick(raw, "trade_date") || ""),
      price: num(pick(raw, "price", "last_price")),
      direction: direction,
      prob: prob,
      confidence: confidence,
      horizon: String(pick(raw, "horizon") || "1d"),
      expiresAt: String(pick(raw, "expires_at") || ""),
      modelVersion: String(pick(raw, "model_version", "model") || ""),
      source: String(pick(raw, "source") || "local"),
      status: status,
      features: normFeatureList(pick(raw, "top_features", "features")),
      paper: obj(pick(raw, "paper"))
    };
  }

  function extractSignals(payload) {
    if (!payload) return [];
    if (Array.isArray(payload)) return payload;
    if (typeof payload === "object") {
      var list = pick(payload, "items", "signals", "data");
      if (Array.isArray(list)) return list;
      if (payload.signal && typeof payload.signal === "object") return [payload.signal];
      if (payload.symbol) return [payload];
    }
    return [];
  }

  function mergeSignals(incoming) {
    if (!incoming.length) return state.signals;
    var map = new Map();
    state.signals.forEach(function (s) { map.set(s.id, s); });
    incoming.forEach(function (s) { map.set(s.id, s); });
    var list = Array.from(map.values());
    list.sort(function (a, b) {
      var ta = Date.parse(a.asof) || 0;
      var tb = Date.parse(b.asof) || 0;
      if (tb !== ta) return tb - ta;
      return String(b.id).localeCompare(String(a.id));
    });
    return list.slice(0, 120);
  }

  function normPaper(raw) {
    if (!raw || typeof raw !== "object") return null;
    var account = obj(pick(raw, "account", "acct"));
    var positions = arr(pick(raw, "positions", "pos")).map(function (p) {
      return {
        symbol: String(pick(p, "symbol", "code") || ""),
        qty: num(pick(p, "qty", "quantity")) || 0,
        available: num(pick(p, "available_qty", "available", "sellable")),
        avgCost: num(pick(p, "avg_cost", "cost", "avg_price")),
        lastPrice: num(pick(p, "last_price", "price", "last")),
        marketValue: num(pick(p, "market_value", "mv")),
        pnl: num(pick(p, "unrealized_pnl", "pnl", "profit"))
      };
    }).filter(function (p) { return p.symbol; });
    var orders = arr(pick(raw, "orders", "recent_orders", "order_list")).map(function (o) {
      return {
        id: String(pick(o, "order_id", "id") || ""),
        symbol: String(pick(o, "symbol", "code") || ""),
        side: String(pick(o, "side") || "").toLowerCase(),
        qty: num(pick(o, "qty", "quantity")),
        targetPrice: num(pick(o, "target_price", "price")),
        filledPrice: num(pick(o, "filled_price", "fill_price")),
        fee: num(pick(o, "fee", "fees")),
        status: String(pick(o, "status") || "").toLowerCase(),
        rejectReason: String(pick(o, "reject_reason") || ""),
        createdAt: String(pick(o, "created_at", "ts") || ""),
        filledAt: String(pick(o, "filled_at") || "")
      };
    }).filter(function (o) { return o.symbol; });
    var trades = obj(pick(raw, "trades", "trade_stats"));
    var closed = num(pick(raw, "closed_trades", "closed", "trades_closed"));
    if (!Number.isFinite(closed)) closed = num(pick(trades, "total", "closed", "count"));
    var wins = num(pick(raw, "winning_trades", "wins"));
    if (!Number.isFinite(wins)) wins = num(pick(trades, "win", "wins", "profitable"));
    var losses = num(pick(raw, "losing_trades", "losses"));
    if (!Number.isFinite(losses)) losses = num(pick(trades, "loss", "losses"));
    var winRate = num(pick(raw, "win_rate", "winrate"));
    if (!Number.isFinite(winRate) && Number.isFinite(closed) && closed > 0 && Number.isFinite(wins)) {
      winRate = wins / closed;
    }
    var initial = num(pick(account, "initial_cash", "initial_capital"));
    if (!Number.isFinite(initial)) initial = num(pick(raw, "initial_cash", "initial_capital"));
    var equity = num(pick(raw, "equity", "total_equity", "net_asset"));
    if (!Number.isFinite(equity)) equity = num(pick(raw, "account.equity"));
    var cash = num(pick(raw, "cash"));
    if (!Number.isFinite(cash)) cash = num(pick(account, "cash"));
    var totalReturn = num(pick(raw, "total_return", "return", "cum_return"));
    if (!Number.isFinite(totalReturn) && Number.isFinite(equity) && Number.isFinite(initial) && initial) {
      totalReturn = equity / initial - 1;
    }
    var snapshot = {
      cash: cash,
      equity: equity,
      marketValue: num(pick(raw, "market_value", "position_value")),
      todayPnl: num(pick(raw, "today_pnl", "daily_pnl", "day_pnl")),
      realizedPnl: num(pick(raw, "realized_pnl")),
      totalReturn: totalReturn,
      initialCash: initial,
      winRate: winRate,
      closedTrades: closed,
      wins: wins,
      losses: losses,
      asOf: String(pick(raw, "as_of", "asof", "updated_at", "date") || ""),
      positions: positions,
      orders: orders
    };
    var hasData = [snapshot.cash, snapshot.equity, snapshot.marketValue].some(Number.isFinite) ||
      positions.length > 0 || orders.length > 0;
    return hasData ? snapshot : null;
  }

  function normEquity(payload) {
    var list = [];
    if (Array.isArray(payload)) list = payload;
    else if (payload && typeof payload === "object") {
      list = arr(pick(payload, "items", "points", "curve", "equity_curve", "data"));
      if (!list.length && Array.isArray(payload.dates) && Array.isArray(payload.equity)) {
        list = payload.dates.map(function (d, i) { return { ts: d, equity: payload.equity[i] }; });
      }
    }
    var points = list.map(function (p) {
      if (Array.isArray(p)) return { ts: String(p[0] || ""), equity: num(p[1]) };
      if (!p || typeof p !== "object") return null;
      var eq = num(pick(p, "equity", "nav", "value", "total_equity"));
      var ts = pick(p, "ts", "time", "trade_date", "date", "asof");
      if (!Number.isFinite(eq) || !ts) return null;
      return {
        ts: String(ts),
        equity: eq,
        cash: num(pick(p, "cash")),
        marketValue: num(pick(p, "market_value", "mv"))
      };
    }).filter(Boolean);
    points.sort(function (a, b) {
      var ta = Date.parse(a.ts) || 0, tb = Date.parse(b.ts) || 0;
      return ta - tb;
    });
    var dedup = [];
    points.forEach(function (p) {
      var last = dedup[dedup.length - 1];
      if (last && last.ts === p.ts) dedup[dedup.length - 1] = p;
      else dedup.push(p);
    });
    return dedup;
  }

  function normBacktest(raw) {
    if (!raw || typeof raw !== "object") return null;
    var meta = obj(pick(raw, "meta"));
    var direction = obj(pick(raw, "direction", "metrics"));
    var win = obj(pick(raw, "win_rate", "trade"));
    var benchmarksRaw = obj(pick(raw, "benchmarks"));
    var perClass = obj(pick(direction, "per_class", "by_direction", "per_direction"));
    var calRaw = arr(pick(direction, "calibration", "calibration_curve", "reliability"));
    var calibration = calRaw.map(function (b) {
      if (!b || typeof b !== "object") return null;
      var label = String(pick(b, "bucket", "label", "bin") || "");
      var n = num(pick(b, "n", "count", "samples"));
      var hit = asRatio(pick(b, "hit_rate", "hit", "accuracy", "observed"));
      var conf = asRatio(pick(b, "avg_confidence", "confidence", "pred", "predicted"));
      return { label: label, n: n, hit: hit, conf: conf, share: num(pick(b, "share")) };
    }).filter(Boolean);

    function dirMetrics(key) {
      var d = obj(perClass[key]);
      var hit = asRatio(pick(d, "precision", "hit_rate", "accuracy"));
      return {
        hit: hit,
        recall: asRatio(pick(d, "recall")),
        support: num(pick(d, "support", "n", "count")),
        predicted: num(pick(d, "predicted"))
      };
    }

    var comparison = arr(pick(benchmarksRaw, "comparison"));
    var comparisonMap = {};
    comparison.forEach(function (c) { if (c && c.benchmark) comparisonMap[c.benchmark] = c; });
    var benchmarks = ["always_flat", "majority", "ma5_cross", "random"].map(function (key) {
      var bench = obj(benchmarksRaw[key]);
      if (!Object.keys(bench).length) return null;
      var cmp = obj(comparisonMap[key]);
      return {
        key: key,
        label: BENCH_LABEL[key] || key,
        accuracy: asRatio(pick(bench, "accuracy")),
        winRate: asRatio(pick(bench, "win_rate")),
        totalReturn: asRatio(pick(bench, "total_return")),
        diffPp: num(pick(cmp, "accuracy_diff_pp")),
        verdict: String(pick(cmp, "verdict") || "")
      };
    }).filter(Boolean);

    var eqCurve = obj(pick(win, "equity_curve"));
    var normalizedCurve = normEquity(pick(win, "equity_curve")) || [];

    var bt = {
      generatedAt: String(pick(raw, "generated_at", "created_at") || ""),
      modelVersion: String(pick(meta, "model_version") || ""),
      range: arr(pick(meta, "evaluated_range", "eval_range")),
      dataRange: arr(pick(meta, "data_range")),
      symbols: arr(pick(meta, "symbols")),
      n: num(pick(direction, "n")) || num(pick(meta, "n_samples")),
      folds: num(pick(meta, "walk_forward_splits_cfg")) || arr(pick(obj(pick(raw, "walk_forward")), "splits")).length,
      accuracy: asRatio(pick(direction, "accuracy")),
      accCI: normCI(pick(direction, "accuracy_ci95", "accuracy_ci", "ci95")),
      logloss: num(pick(direction, "logloss", "log_loss")),
      brier: num(pick(direction, "brier")),
      coverage: asRatio(pick(direction, "coverage")),
      calibration: calibration,
      perDirection: { up: dirMetrics("up"), flat: dirMetrics("flat"), down: dirMetrics("down") },
      winRate: asRatio(pick(win, "win_rate")),
      winCI: normCI(pick(win, "wilson95", "wilson_ci", "win_rate_ci")),
      sampleSufficient: pick(win, "sample_sufficient") !== false,
      winNote: String(pick(win, "note") || ""),
      trades: obj(pick(win, "trades")),
      avgReturn: asRatio(pick(win, "avg_return_per_trade")),
      payoff: num(pick(win, "payoff_ratio")),
      mdd: asRatio(pick(win, "max_drawdown")),
      sharpe: num(pick(win, "sharpe", "sharpe_ratio")),
      totalReturn: asRatio(pick(win, "total_return")),
      benchmarks: benchmarks,
      warnings: arr(pick(raw, "warnings")),
      equityCurve: normalizedCurve.length ? normalizedCurve : (eqCurve.dates ? normEquity(eqCurve) : []),
      raw: raw
    };
    if (!Number.isFinite(bt.accuracy) && !Number.isFinite(bt.winRate)) return null;
    return bt;
  }

  function wilson(successes, n) {
    if (!Number.isFinite(successes) || !Number.isFinite(n) || n <= 0) return null;
    var z = 1.959963984540054, z2 = z * z;
    var p = successes / n;
    var denom = 1 + z2 / n;
    var center = (p + z2 / (2 * n)) / denom;
    var half = (z * Math.sqrt((p * (1 - p)) / n + z2 / (4 * n * n))) / denom;
    return { low: Math.max(0, center - half), high: Math.min(1, center + half) };
  }

  /* ============================== 空状态 ============================== */
  function emptyNode(conf) {
    var wrap = document.createElement("div");
    wrap.className = "empty";
    var title = document.createElement("div");
    title.className = "empty-title";
    title.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#' + esc(conf.icon || "i-info") + '"/></svg>';
    var label = document.createElement("span");
    label.textContent = conf.title || "";
    title.appendChild(label);
    wrap.appendChild(title);
    if (conf.message) {
      var p = document.createElement("p");
      p.textContent = conf.message;
      wrap.appendChild(p);
    }
    if (conf.command) {
      var code = document.createElement("code");
      code.textContent = conf.command;
      wrap.appendChild(code);
    }
    if (conf.note) {
      var note = document.createElement("p");
      note.className = "empty-note";
      note.textContent = conf.note;
      wrap.appendChild(note);
    }
    return wrap;
  }

  function renderInto(el, conf) {
    if (!el) return;
    el.replaceChildren(emptyNode(conf));
  }

  function signalEmptyConf() {
    if (state.dev && !IS_CLOUD) {
      return {
        title: "静态预览 · 未连接 API",
        message: "当前没有 /api/signals 服务（纯静态预览会以开发模式降级）。",
        command: "python -m quant.cli serve --port 8787",
        note: "启动本地数据服务后刷新页面即可看到真实信号与指标。",
        icon: "i-warning"
      };
    }
    if (IS_CLOUD) {
      return {
        title: "等待云端信号",
        message: "暂无云端信号数据。",
        command: "python -m quant.cli export --target cloudflare",
        note: "本地推送后，SSE 会实时刷新方向信号与信号表。",
        icon: "i-warning"
      };
    }
    return {
      title: "暂无信号",
      message: "signals 表为空时会兜底读取 data/reports/signals.json。",
      command: "python -m quant.cli signal",
      note: "生成信号后点击右上角刷新；页面每 60s 自动轮询。",
      icon: "i-warning"
    };
  }

  /* ============================== 渲染：状态条 ============================== */
  var CONN_TEXT = {
    connecting: "连接中",
    live: IS_CLOUD ? "SSE 已连接" : "已连接",
    degraded: "部分接口异常",
    reconnecting: "重连中",
    offline: "连接断开"
  };

  function setConn(status) {
    state.conn = status;
    var conn = $("#connState");
    if (conn) conn.dataset.state = status;
    var bar = $("#connBar");
    if (bar) bar.dataset.state = status;
    if (B["conn.text"]) setText(B["conn.text"], CONN_TEXT[status] || "连接中");
  }

  function renderStatus() {
    var modelVersion = "";
    if (state.signals.length) modelVersion = state.signals[0].modelVersion;
    if (!modelVersion && state.backtest) modelVersion = state.backtest.modelVersion;
    if (B["status.model"]) setText(B["status.model"], modelVersion ? "model " + modelVersion : "model " + DASH);
    if (B["status.asof"]) {
      var stamp = state.lastAsof ? shParts(state.lastAsof) : null;
      var text = state.lastOkAt
        ? "最后更新 " + fmtHM(new Date(state.lastOkAt))
        : "最后更新 " + DASH;
      setText(B["status.asof"], text);
      B["status.asof"].title = stamp
        ? "信号时间 " + fmtFullTime(state.lastAsof)
        : (state.lastOkAt ? "最近一次成功拉取 " + fmtFullTime(new Date(state.lastOkAt)) : "尚未成功拉取");
    }
    if (B["foot.updated"]) {
      setText(B["foot.updated"], state.lastOkAt ? "更新 " + fmtHMS(new Date(state.lastOkAt)) : "更新 " + DASH);
    }
  }

  function updateSession() {
    var p = shParts(new Date());
    var session = $("#session");
    if (!p || !session) return;
    var weekend = p.wd === "Sat" || p.wd === "Sun";
    var mins = Number(p.hh) * 60 + Number(p.mm);
    var label = "已收盘", mode = "closed";
    if (weekend) { label = "周末休市"; }
    else if (mins < 9 * 60 + 15) { label = "未开盘"; }
    else if (mins < 9 * 60 + 30) { label = "集合竞价"; mode = "auction"; }
    else if (mins < 11 * 60 + 30) { label = "交易中"; mode = "open"; }
    else if (mins < 13 * 60) { label = "午间休市"; }
    else if (mins < 15 * 60) { label = "交易中"; mode = "open"; }
    session.dataset.state = mode;
    if (B["status.phase"]) setText(B["status.phase"], label);
  }

  function announce(text) {
    var live = $("#liveRegion");
    if (!live || state.hidden) return;
    live.textContent = "";
    setTimeout(function () { live.textContent = text; }, 60);
  }

  /* ============================== 渲染：方向信号卡 ============================== */
  var lastDirKey = "";

  function dirView(sig) {
    if (sig.status === "warming_up") return { dir: "flat", label: "数据不足" };
    return { dir: sig.direction, label: DIR_LABEL[sig.direction] || DASH };
  }

  function statusTag(sig) {
    if (sig.status === "stale") return { cls: "tag tag-warn", label: "非交易时段", short: "过期" };
    if (sig.status === "warming_up") return { cls: "tag tag-flat", label: "数据不足", short: "预热" };
    return { cls: "tag", label: "信号有效", short: "有效" };
  }

  function statusTagHtml(tag) {
    return '<span class="' + tag.cls + '"><span class="st-full">' + esc(tag.label) +
      '</span><span class="st-short">' + esc(tag.short || tag.label) + "</span></span>";
  }

  function renderSignalTabs(symbols) {
    var host = $("#sigTabs");
    if (!host) return;
    var signature = symbols.map(function (s) { return s.symbol; }).join(",");
    if (host.dataset.signature === signature) {
      $$(".sym-tab", host).forEach(function (btn) {
        btn.setAttribute("aria-selected", btn.dataset.symbol === state.selected ? "true" : "false");
      });
      return;
    }
    host.dataset.signature = signature;
    host.replaceChildren();
    symbols.forEach(function (s) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "sym-tab";
      btn.dataset.symbol = s.symbol;
      btn.setAttribute("role", "tab");
      btn.setAttribute("aria-selected", s.symbol === state.selected ? "true" : "false");
      btn.innerHTML = "<span>" + esc(s.symbol) + '</span><span class="sym-name">' + esc(s.name || DASH) + "</span>";
      btn.addEventListener("click", function () {
        if (state.selected === s.symbol) return;
        state.selected = s.symbol;
        renderSignalCard();
      });
      host.appendChild(btn);
    });
  }

  function renderFeatures(features) {
    var host = L.features;
    if (!host) return;
    if (!features.length) {
      host.dataset.signature = "";
      host.innerHTML = '<div class="inline-empty">无特征贡献数据。</div>';
      return;
    }
    var maxAbs = features.reduce(function (m, f) { return Math.max(m, Math.abs(f.contrib)); }, 0) || 1;
    var signature = features.map(function (f) { return f.name + ":" + f.contrib; }).join("|");
    if (host.dataset.signature === signature && host.querySelector(".feat-row")) {
      $$(".feat-row", host).forEach(function (row) {
        var bar = row.querySelector(".feat-track > i");
        var f = features[Number(row.dataset.index)];
        if (!f || !bar) return;
        bar.style.transform = "scaleX(" + clamp(Math.abs(f.contrib) / maxAbs, 0, 1) + ")";
      });
      return;
    }
    host.dataset.signature = signature;
    host.replaceChildren();
    features.forEach(function (f, i) {
      var sign = f.contrib > 0 ? "pos" : f.contrib < 0 ? "neg" : "zero";
      var row = document.createElement("div");
      row.className = "feat-row";
      row.dataset.sign = sign;
      row.dataset.index = String(i);
      row.innerHTML =
        '<span class="feat-name" title="' + esc(f.name) + '">' + esc(f.name) + "</span>" +
        '<span class="feat-track"><i></i></span>' +
        '<span class="feat-val">' + esc(fmtFeature(f.value)) + "</span>" +
        '<span class="feat-contrib ' + (f.contrib > 0 ? "is-pos" : f.contrib < 0 ? "is-neg" : "") + '">' +
        esc((f.contrib > 0 ? "+" : "") + f.contrib.toFixed(3)) + "</span>";
      host.appendChild(row);
      var bar = row.querySelector(".feat-track > i");
      var scale = clamp(Math.abs(f.contrib) / maxAbs, 0.02, 1);
      bar.style.transform = "scaleX(0)";
      requestAnimationFrame(function () { bar.style.transform = "scaleX(" + scale + ")"; });
    });
  }

  function setProbBar(prob) {
    var segs = $$("#probBar .seg");
    var order = ["up", "flat", "down"];
    var offset = 0;
    var values = order.map(function (k) { return clamp(prob[k] || 0, 0, 1); });
    var sum = values[0] + values[1] + values[2] || 1;
    values = values.map(function (v) { return v / sum; });
    segs.forEach(function (seg, i) {
      var p = values[i];
      var target = "translateX(" + (offset * 100).toFixed(3) + "%) scaleX(" + Math.max(p, 0.0001).toFixed(4) + ")";
      if (seg.dataset.applied === target) { offset += p; return; }
      var from = seg.dataset.applied || "translateX(0%) scaleX(0)";
      seg.dataset.applied = target;
      if (reduced() || typeof seg.animate !== "function") { seg.style.transform = target; offset += p; return; }
      try {
        seg.animate([{ transform: from }, { transform: target }],
          { duration: 240, easing: "cubic-bezier(0.22, 1, 0.36, 1)", fill: "forwards" });
      } catch (e) { seg.style.transform = target; }
      offset += p;
    });
  }

  function renderSignalCard() {
    var host = $("#signalEmpty");
    var body = $("#signalBody");
    var sorted = state.signals;
    var symbols = [];
    var seen = {};
    sorted.forEach(function (s) {
      if (!seen[s.symbol]) { seen[s.symbol] = true; symbols.push({ symbol: s.symbol, name: s.name }); }
    });
    if (!sorted.length) {
      if (body) body.hidden = true;
      show(host, true);
      renderInto(host, signalEmptyConf());
      if (B["signal.meta"]) setText(B["signal.meta"], "model " + DASH);
      if (B["signal.tradeDate"]) setText(B["signal.tradeDate"], DASH);
      if (B["signal.foot"]) setText(B["signal.foot"], "暂无可用信号");
      renderSignalTabs([]);
      return;
    }
    if (body) body.hidden = false;
    show(host, false);

    if (!state.selected || !seen[state.selected]) state.selected = sorted[0].symbol;
    renderSignalTabs(symbols);

    var sig = null;
    for (var i = 0; i < sorted.length; i += 1) {
      if (sorted[i].symbol === state.selected) { sig = sorted[i]; break; }
    }
    if (!sig) sig = sorted[0];

    var view = dirView(sig);
    var dirEl = $("#sigDirWord");
    if (dirEl) {
      var dirKey = sig.id + "|" + view.label;
      if (dirEl.textContent !== view.label) {
        dirEl.textContent = view.label;
        if (!reduced()) {
          animate(dirEl, [
            { opacity: 0.35, transform: "scale(0.98)" },
            { opacity: 1, transform: "scale(1)" }
          ], { duration: 220, easing: "cubic-bezier(0.22, 1, 0.36, 1)" });
        }
      }
      dirEl.dataset.dir = view.dir;
      lastDirKey = dirKey;
    }
    var lamp = $("#sigLamp");
    if (lamp) lamp.dataset.dir = view.dir;

    var tags = $("#sigStatusTag");
    if (tags) {
      var tag = statusTag(sig);
      var tagKey = sig.id + "|" + tag.label;
      if (tags.dataset.signature !== tagKey) {
        tags.dataset.signature = tagKey;
        tags.innerHTML = '<span class="' + tag.cls + '">' + esc(tag.label) + "</span>" +
          (sig.confidence < 0.4 && sig.status !== "warming_up" ? '<span class="tag tag-warn">低置信</span>' : "");
      }
    }

    tweenNumber($("#sigPrice"), sig.price, fmtPrice);
    if ($("#sigPriceMeta")) {
      setText($("#sigPriceMeta"), fmtMDHM(sig.asof) + " · " + (sig.source === "cloud" ? "CLOUD" : "LOCAL"));
    }

    if (B["signal.meta"]) setText(B["signal.meta"], "model " + (sig.modelVersion || DASH) + " · " + sig.horizon);
    if (B["signal.tradeDate"]) setText(B["signal.tradeDate"], sig.tradeDate || DASH);
    if (B["sig.horizon"]) setText(B["sig.horizon"], sig.horizon);
    if (B["sig.expires"]) setText(B["sig.expires"], sig.expiresAt ? fmtMDHM(sig.expiresAt) : DASH);
    if (B["sig.source"]) setText(B["sig.source"], sig.source === "cloud" ? "云端" : "本地");

    setProbBar(sig.prob);
    tweenNumber($("#probUp"), sig.prob.up, function (v) { return fmtPct(v); });
    tweenNumber($("#probFlat"), sig.prob.flat, function (v) { return fmtPct(v); });
    tweenNumber($("#probDown"), sig.prob.down, function (v) { return fmtPct(v); });

    var maxKey = sig.prob.up >= sig.prob.flat && sig.prob.up >= sig.prob.down
      ? "up" : sig.prob.down >= sig.prob.flat ? "down" : "flat";
    $$(".prob-item").forEach(function (item) {
      item.classList.toggle("is-max", item.dataset.key === maxKey);
    });
    var bar = $("#probBar");
    if (bar) {
      bar.setAttribute("aria-label",
        "看涨 " + fmtPct(sig.prob.up) + "，震荡 " + fmtPct(sig.prob.flat) + "，看跌 " + fmtPct(sig.prob.down));
    }

    tweenNumber($("#confValue"), sig.confidence, function (v) { return fmtPct(v); });
    var confBar = $("#confBar") && $("#confBar").firstElementChild;
    if (confBar) confBar.style.transform = "scaleX(" + clamp(sig.confidence, 0, 1) + ")";
    show($("#confLow"), sig.confidence < 0.4 && sig.status !== "warming_up");

    renderFeatures(sig.features);

    if (B["signal.foot"]) {
      var foot = "信号 " + fmtFullTime(sig.asof) + " 生成";
      if (sig.expiresAt) foot += " · 有效期至 " + fmtMDHM(sig.expiresAt);
      foot += " · " + sig.symbol + " " + (sig.name || DASH);
      setText(B["signal.foot"], foot);
    }
  }

  /* ============================== 渲染：信号表（FLIP） ============================== */
  function signalRowHtml(sig) {
    var view = dirView(sig);
    var tag = statusTag(sig);
    var p = shParts(sig.asof);
    var timeHtml = p
      ? '<span class="t-date">' + p.m + "-" + p.d + "</span> " + p.hh + ":" + p.mm
      : esc(fmtMDHM(sig.asof));
    return '<td class="col-time"><span class="t-time">' + timeHtml + "</span></td>" +
      '<td class="col-sym"><span class="mono">' + esc(sig.symbol) + "</span>" +
      '<span class="sym-name-m">' + esc(sig.name || DASH) + "</span></td>" +
      '<td class="col-name">' + esc(sig.name || DASH) + "</td>" +
      '<td class="col-dir"><span class="cell-dir" data-dir="' + view.dir + '"><i class="dot"></i>' +
      esc(view.label) + "</span></td>" +
      '<td class="col-prob num">' + esc(fmtPct(sig.prob.up)) + "</td>" +
      '<td class="col-conf num">' + esc(fmtPct(sig.confidence)) + "</td>" +
      '<td class="col-price num">' + esc(fmtPrice(sig.price)) + "</td>" +
      '<td class="col-status">' + statusTagHtml(tag) + "</td>";
  }

  function hydrateSignalRow(tr, sig) {
    tr.innerHTML = signalRowHtml(sig);
    tr.dataset.status = sig.status;
    tr.setAttribute("data-flip-id", sig.id);
    /* 记录初始数值，下一次轮询才能从旧值平滑补间 */
    var seed = [
      [".col-prob", sig.prob.up],
      [".col-conf", sig.confidence],
      [".col-price", sig.price]
    ];
    seed.forEach(function (pair) {
      var cell = tr.querySelector(pair[0]);
      if (cell && isNum(pair[1])) { cell.__num = pair[1]; cell.__cur = pair[1]; }
    });
  }

  function updateSignalRow(tr, sig, prev) {
    if (!prev) { hydrateSignalRow(tr, sig); return; }
    tr.dataset.status = sig.status;
    if (prev.asof !== sig.asof) {
      var timeCell = tr.querySelector(".col-time");
      if (timeCell) {
        var p = shParts(sig.asof);
        timeCell.innerHTML = '<span class="t-time">' + (p
          ? '<span class="t-date">' + p.m + "-" + p.d + "</span> " + p.hh + ":" + p.mm
          : esc(fmtMDHM(sig.asof))) + "</span>";
      }
    }
    if (prev.direction !== sig.direction || prev.status !== sig.status || prev.name !== sig.name) {
      var view = dirView(sig);
      var dirCell = tr.querySelector(".col-dir");
      if (dirCell) {
        dirCell.innerHTML = '<span class="cell-dir" data-dir="' + view.dir + '"><i class="dot"></i>' +
          esc(view.label) + "</span>";
        animate(dirCell, [{ opacity: 0.4 }, { opacity: 1 }], { duration: 220, easing: "ease-out" });
      }
      var tag = statusTag(sig);
      var statusCell = tr.querySelector(".col-status");
      if (statusCell) statusCell.innerHTML = statusTagHtml(tag);
    }
    tweenNumber(tr.querySelector(".col-prob"), sig.prob.up, function (v) { return fmtPct(v); });
    tweenNumber(tr.querySelector(".col-conf"), sig.confidence, function (v) { return fmtPct(v); });
    tweenNumber(tr.querySelector(".col-price"), sig.price, fmtPrice);
  }

  function renderSignalTable() {
    var tbody = L.signals;
    if (!tbody) return;
    var rows = state.signals.slice(0, MAX_SIGNAL_ROWS);
    var wrap = $("#signalsWrap");
    var empty = $("#signalsEmpty");
    if (!rows.length) {
      state.rows.forEach(function (rec) { rec.tr.remove(); });
      state.rows.clear();
      state.rendered.sigTable = "";
      if (wrap) wrap.hidden = true;
      show(empty, true);
      renderInto(empty, signalEmptyConf());
      if (B["signals.meta"]) setText(B["signals.meta"], "0 条");
      if (B["signals.foot"]) setText(B["signals.foot"], "暂无信号记录");
      return;
    }
    if (wrap) wrap.hidden = false;
    show(empty, false);

    var signature = rows.map(function (s) {
      return s.id + "|" + s.price + "|" + s.prob.up + "|" + s.confidence + "|" + s.direction + "|" + s.status + "|" + s.asof;
    }).join(";");
    if (signature === state.rendered.sigTable) return;
    state.rendered.sigTable = signature;

    /* 首次渲染前清掉骨架屏行 */
    Array.prototype.slice.call(tbody.children).forEach(function (tr) {
      if (!tr.getAttribute("data-flip-id")) tr.remove();
    });

    var prevTops = new Map();
    state.rows.forEach(function (rec, id) {
      if (rec.tr.isConnected) prevTops.set(id, rec.tr.getBoundingClientRect().top);
    });

    var seen = new Set();
    var frag = document.createDocumentFragment();
    var newRows = [];
    rows.forEach(function (sig) {
      seen.add(sig.id);
      var rec = state.rows.get(sig.id);
      if (!rec) {
        var tr = document.createElement("tr");
        hydrateSignalRow(tr, sig);
        rec = { tr: tr, sig: sig };
        state.rows.set(sig.id, rec);
        newRows.push(tr);
      } else {
        updateSignalRow(rec.tr, sig, rec.sig);
        rec.sig = sig;
      }
      frag.appendChild(rec.tr);
    });
    state.rows.forEach(function (rec, id) {
      if (!seen.has(id)) { rec.tr.remove(); state.rows.delete(id); }
    });
    tbody.appendChild(frag);

    if (reduced()) return;
    var easing = "cubic-bezier(0.22, 1, 0.36, 1)";
    state.rows.forEach(function (rec, id) {
      var prev = prevTops.get(id);
      if (prev === undefined || !rec.tr.isConnected) return;
      var now = rec.tr.getBoundingClientRect().top;
      var dy = prev - now;
      if (Math.abs(dy) > 0.5) {
        animate(rec.tr, [{ transform: "translateY(" + dy + "px)" }, { transform: "translateY(0)" }],
          { duration: FLIP_MS, easing: easing });
      }
    });
    newRows.forEach(function (tr) {
      animate(tr, [
        { opacity: 0, transform: "translateY(-6px)" },
        { opacity: 1, transform: "translateY(0)" }
      ], { duration: FLIP_MS, easing: easing });
    });
  }

  /* ============================== 渲染：回测 ============================== */
  function setMetric(elId, value, format, duration) {
    var el = document.getElementById(elId);
    if (el) tweenNumber(el, value, format, duration);
  }

  function benchRowHtml(row) {
    var delta = isNum(row.diffPp) ? fmtPP(row.diffPp) : DASH;
    var deltaCls = isNum(row.diffPp) ? (row.diffPp > 0 ? "is-pos" : row.diffPp < 0 ? "is-neg" : "") : "";
    var flag = row.verdict && row.verdict.indexOf("CI 外") !== -1
      ? '<span class="tag tag-accent bench-flag" title="' + esc(row.verdict) + '">CI外</span>'
      : "";
    return "<tr><td>" + esc(row.label) + flag + "</td>" +
      '<td class="num">' + esc(fmtPct(row.accuracy)) + "</td>" +
      '<td class="num">' + esc(fmtPct(row.winRate)) + "</td>" +
      '<td class="num">' + esc(fmtSignedPct(row.totalReturn)) + "</td>" +
      '<td class="num"><span class="bench-delta ' + deltaCls + '">' + esc(delta) + "</span></td></tr>";
  }

  function buildCalibration(bt) {
    var host = $("#calChart");
    if (!host) return;
    if (!bt.calibration.length) {
      renderInto(host, { title: "无校准数据", message: "回测报告未包含置信度分桶。", icon: "i-chart" });
      return;
    }
    var xMin = 0.3, xMax = 0.8;
    var total = bt.calibration.reduce(function (s, b) { return s + (b.n || 0); }, 0);
    var maxN = bt.calibration.reduce(function (m, b) { return Math.max(m, b.n || 0); }, 1);
    var plot = document.createElement("div");
    plot.className = "cal-plot";
    var diag = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    diag.setAttribute("class", "cal-diag");
    diag.setAttribute("viewBox", "0 0 100 100");
    diag.setAttribute("preserveAspectRatio", "none");
    diag.setAttribute("aria-hidden", "true");
    var line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", "0"); line.setAttribute("y1", "100");
    line.setAttribute("x2", "100"); line.setAttribute("y2", "0");
    diag.appendChild(line);
    plot.appendChild(diag);

    var xTicks = [0.4, 0.5, 0.6, 0.7];
    [0.5, 1].forEach(function (t) {
      var y = document.createElement("span");
      y.className = "cal-ylabel";
      y.style.top = ((1 - t) * 100) + "%";
      y.textContent = Math.round(t * 100) + "%";
      plot.appendChild(y);
    });

    bt.calibration.forEach(function (b) {
      if (!isNum(b.hit)) return;
      var conf = isNum(b.conf) ? b.conf : midpoint(b.label);
      if (!isNum(conf)) return;
      var x = clamp((conf - xMin) / (xMax - xMin), 0, 1);
      var y = clamp(b.hit, 0, 1);
      var size = 7 + 6 * Math.sqrt((b.n || 0) / maxN);
      var pt = document.createElement("button");
      pt.type = "button";
      pt.className = "cal-pt";
      pt.style.left = (x * 100) + "%";
      pt.style.bottom = (y * 100) + "%";
      pt.style.width = size.toFixed(1) + "px";
      pt.style.height = size.toFixed(1) + "px";
      pt.setAttribute("aria-label",
        "置信度桶 " + (b.label || fmtPct(conf)) + "：命中率 " + fmtPct(b.hit) + "，样本 " + fmtInt(b.n));
      var tip = document.createElement("span");
      tip.className = "cal-tip";
      tip.textContent = (b.label || fmtPct(conf)) + " · 命中 " + fmtPct(b.hit) + " · n=" + fmtInt(b.n) +
        (isNum(b.conf) ? " · 平均置信 " + fmtPct(b.conf) : "");
      pt.appendChild(tip);
      plot.appendChild(pt);
    });

    var xaxis = document.createElement("div");
    xaxis.className = "cal-xaxis";
    xaxis.setAttribute("aria-hidden", "true");
    xTicks.forEach(function (t) {
      var l = document.createElement("span");
      l.style.left = (((t - xMin) / (xMax - xMin)) * 100) + "%";
      l.textContent = Math.round(t * 100) + "%";
      xaxis.appendChild(l);
    });
    host.replaceChildren(plot, xaxis);
    if ($("#calNote")) {
      setText($("#calNote"), "共 " + fmtInt(total) + " 个样本；点越大样本越多。完美校准时点落在 45° 虚线上（命中率 = 置信度），越靠上越保守、越靠下越自信。");
    }
  }

  function midpoint(label) {
    var s = String(label || "");
    var nums = s.match(/[\d.]+/g);
    if (!nums || !nums.length) return undefined;
    if (nums.length >= 2) return (parseFloat(nums[0]) + parseFloat(nums[1])) / 2;
    var v = parseFloat(nums[0]);
    if (!Number.isFinite(v)) return undefined;
    if (s.indexOf("<") !== -1 || s.indexOf("≤") !== -1) return v - 0.05;
    return v + 0.05;
  }

  function ensureDirRows() {
    if (!L.dirs) return;
    if (L.dirs.querySelector('.dir-row[data-dir="up"]')) return;
    L.dirs.innerHTML = ["up", "flat", "down"].map(function (key) {
      return '<div class="dir-row" data-dir="' + key + '">' +
        '<span class="dir-name"><i class="dot"></i>' + DIR_LABEL[key] + "</span>" +
        '<span class="dir-track"><i></i></span>' +
        '<span class="dir-hit">' + DASH + "</span>" +
        '<span class="dir-n">' + DASH + "</span></div>";
    }).join("");
  }

  function renderBacktest() {
    ensureDirRows();
    var content = $("#btContent");
    var empty = $("#btEmpty");
    var bt = state.backtest;
    if (!bt) {
      if (content) content.hidden = true;
      show(empty, true);
      var conf = state.backtestError || {};
      if (IS_CLOUD) {
        conf = {
          title: "云端不提供回测报告",
          message: "回测数据由本地研究环境生成。",
          command: "python -m quant.cli backtest --start 2025-01-01 --end 2026-11-20",
          note: "运行后查看本地看板 http://127.0.0.1:8787/。",
          icon: "i-chart"
        };
      } else if (!conf.message && state.dev) {
        conf = {
          title: "静态预览 · 未连接数据服务",
          message: "启动本地数据服务后可查看回测报告。",
          command: "python -m quant.cli serve --port 8787",
          note: "或先运行 python -m quant.cli backtest 生成报告。",
          icon: "i-chart"
        };
      } else if (!conf.message) {
        conf = {
          title: "未回测",
          message: "尚未生成回测报告。",
          command: "python -m quant.cli backtest --start 2025-01-01 --end 2026-11-20",
          note: "报告写入 data/reports/backtest.json，刷新后展示指标与校准图。",
          icon: "i-chart"
        };
      }
      renderInto(empty, conf);
      if (B["bt.meta"]) setText(B["bt.meta"], DASH);
      if (B["bt.range"]) setText(B["bt.range"], DASH);
      if (B["bt.foot"]) setText(B["bt.foot"], "回测报告不可用");
      $$("#dirList .dir-row").forEach(function (row) {
        row.querySelector(".dir-hit").textContent = DASH;
        row.querySelector(".dir-track > i").style.transform = "scaleX(0)";
      });
      if (L.benchmarks) L.benchmarks.replaceChildren();
      renderInto($("#calChart"), { title: "未回测", message: "等待 backtest.json。", icon: "i-chart" });
      if ($("#calNote")) setText($("#calNote"), "运行回测后显示置信度分桶与命中率。");
      return;
    }
    if (content) content.hidden = false;
    show(empty, false);
    state.backtestError = null;

    var range = bt.range.length >= 2 ? bt.range : bt.dataRange;
    if (B["bt.meta"]) {
      setText(B["bt.meta"], (bt.n ? "n=" + fmtInt(bt.n) : "n=" + DASH) +
        " · 收益 " + fmtSignedPct(bt.totalReturn));
    }
    if (B["bt.range"]) {
      setText(B["bt.range"], range.length >= 2 ? rawDate(range[0]) + " → " + rawDate(range[1]) : DASH);
    }

    setMetric("btAcc", bt.accuracy, function (v) { return fmtPct(v); });
    var accCI = bt.accCI;
    if (!accCI && Number.isFinite(bt.accuracy) && Number.isFinite(bt.n) && bt.n > 0) {
      accCI = wilson(bt.accuracy * bt.n, bt.n);
    }
    setText($("#btAccSub"), accCI
      ? "95% CI " + fmtPct(accCI.low) + "–" + fmtPct(accCI.high) + (bt.n ? " · n=" + fmtInt(bt.n) : "")
      : (bt.n ? "n=" + fmtInt(bt.n) : DASH));

    setMetric("btWin", bt.winRate, function (v) { return fmtPct(v); });
    var trades = bt.trades;
    var winFlag = $("#btWinFlag");
    if (winFlag) winFlag.hidden = !(Number.isFinite(bt.winRate) && bt.sampleSufficient === false);
    if (!Number.isFinite(bt.winRate)) {
      setText($("#btWinSub"), "无平仓样本");
    } else if (bt.winCI) {
      var closed = num(trades.total);
      setText($("#btWinSub"), "95% CI " + fmtPct(bt.winCI.low) + "–" + fmtPct(bt.winCI.high) +
        (Number.isFinite(closed) ? " · " + fmtInt(closed) + " 笔" : ""));
    } else {
      setText($("#btWinSub"), Number.isFinite(num(trades.total)) ? fmtInt(trades.total) + " 笔" : DASH);
    }

    setMetric("btLogloss", bt.logloss, function (v) { return fmtFixed(v, 3); });
    setText($("#btLoglossSub"), "均匀基准 ln3 = 1.0986" + (isNum(bt.brier) ? " · Brier " + fmtFixed(bt.brier, 3) : ""));

    setMetric("btMdd", bt.mdd, function (v) { return fmtSignedPct(v); });
    tone($("#btMdd"), bt.mdd);
    setText($("#btMddSub"), "组合净值峰谷回撤");

    setMetric("btSharpe", bt.sharpe, function (v) { return fmtFixed(v, 2); });
    setText($("#btSharpeSub"), "日频年化 · rf 0" + (bt.folds ? " · " + bt.folds + " 折" : ""));

    var dirRows = $$("#dirList .dir-row");
    dirRows.forEach(function (row) {
      var key = row.dataset.dir;
      var m = bt.perDirection[key] || {};
      var hitEl = row.querySelector(".dir-hit");
      var nEl = row.querySelector(".dir-n");
      var bar = row.querySelector(".dir-track > i");
      if (hitEl) tweenNumber(hitEl, m.hit, function (v) { return fmtPct(v); });
      if (nEl) setText(nEl, Number.isFinite(m.support) ? "n=" + fmtInt(m.support) : "n=" + DASH);
      if (bar) bar.style.transform = "scaleX(" + clamp(isNum(m.hit) ? m.hit : 0, 0, 1) + ")";
      row.title = Number.isFinite(m.recall)
        ? "precision " + fmtPct(m.hit) + " · recall " + fmtPct(m.recall) + " · 预测数 " + fmtInt(m.predicted)
        : "";
    });

    if (L.benchmarks) {
      var strategy = {
        key: "strategy", label: "策略 strategy", accuracy: bt.accuracy, winRate: bt.winRate,
        totalReturn: bt.totalReturn, diffPp: 0, verdict: ""
      };
      var rows = [strategy].concat(bt.benchmarks);
      L.benchmarks.innerHTML = rows.map(function (row, i) {
        var tr = benchRowHtml(row).replace("<tr>", '<tr data-role="' + (i === 0 ? "strategy" : "benchmark") + '">');
        return tr;
      }).join("");
    }

    buildCalibration(bt);

    if (B["bt.foot"]) {
      var foot = [];
      if (bt.modelVersion) foot.push("模型 " + bt.modelVersion);
      if (bt.symbols.length) foot.push("股票池 " + bt.symbols.join(", "));
      if (bt.generatedAt) foot.push("生成 " + fmtFullTime(bt.generatedAt));
      if (arr(bt.warnings).length) foot.push("警告 " + bt.warnings.length + " 条");
      setText(B["bt.foot"], foot.join(" · ") || DASH);
    }
  }

  /* ============================== 渲染：模拟盘 / 订单 ============================== */
  function renderPaper() {
    var paper = state.paper;
    var content = $("#paperContent");
    var empty = $("#paperEmpty");
    if (!paper) {
      if (content) content.hidden = true;
      show(empty, true);
      renderInto(empty, state.dev && !IS_CLOUD ? {
        title: "静态预览 · 未连接数据服务",
        message: "启动本地数据服务后可查看账户、持仓与胜率。",
        command: "python -m quant.cli serve --port 8787",
        note: "本地看板：http://127.0.0.1:8787/",
        icon: "i-warning"
      } : IS_CLOUD ? {
        title: "云端不提供模拟盘",
        message: "模拟盘数据由本地研究环境提供。",
        command: "python -m quant.cli serve --port 8787",
        note: "本地打开 http://127.0.0.1:8787/ 查看账户与净值。",
        icon: "i-warning"
      } : {
        title: "模拟盘尚无快照",
        message: "初始化账户并推进一天后，这里显示现金、持仓与胜率。",
        command: "python -m quant.cli paper --init-cash 1000000 --advance 1",
        note: "数据库写入后 60s 内自动刷新。",
        icon: "i-warning"
      });
      if (B["paper.meta"]) setText(B["paper.meta"], DASH);
      if (B["paper.foot"]) setText(B["paper.foot"], "暂无模拟盘数据");
      if ($("#paperRetAbs")) setText($("#paperRetAbs"), DASH);
      return;
    }
    if (content) content.hidden = false;
    show(empty, false);

    setMetric("paperCash", paper.cash, fmtMoney);
    setMetric("paperMv", paper.marketValue, fmtMoney);
    setMetric("paperToday", paper.todayPnl, function (v) { return fmtSignedMoney(v); });
    tone($("#paperToday"), paper.todayPnl);
    setMetric("paperRet", paper.totalReturn, function (v) { return fmtSignedPct(v); });
    tone($("#paperRet"), paper.totalReturn);
    setMetric("paperWin", paper.winRate, function (v) { return fmtPct(v); });

    var posCount = paper.positions.length;
    if ($("#paperPosCount")) setText($("#paperPosCount"), "持仓 " + fmtInt(posCount) + " 只");
    if ($("#paperRetAbs")) {
      var abs = Number.isFinite(paper.initialCash) && Number.isFinite(paper.equity)
        ? paper.equity - paper.initialCash : undefined;
      setText($("#paperRetAbs"), Number.isFinite(abs) ? fmtSignedMoney(abs) + " / 初始 " + fmtMoney0(paper.initialCash) : DASH);
    }
    if ($("#paperWinSub")) {
      var parts = [];
      if (Number.isFinite(paper.closedTrades) && paper.closedTrades > 0) {
        parts.push("已平仓 " + fmtInt(paper.closedTrades) + " 笔");
        if (Number.isFinite(paper.wins) && Number.isFinite(paper.losses)) {
          parts.push(fmtInt(paper.wins) + " 胜 / " + fmtInt(paper.losses) + " 负");
        }
        if (paper.closedTrades < 30) parts.push("样本不足");
      }
      setText($("#paperWinSub"), parts.join(" · ") || "无平仓记录");
    }
    if (B["paper.meta"]) setText(B["paper.meta"], paper.asOf ? "as_of " + rawDate(paper.asOf) : DASH);
    if (B["paper.foot"]) {
      var foot = [];
      if (paper.asOf) foot.push("快照 " + rawDate(paper.asOf));
      if (Number.isFinite(paper.realizedPnl)) foot.push("已实现 " + fmtSignedMoney(paper.realizedPnl));
      if (Number.isFinite(paper.equity)) foot.push("权益 " + fmtMoney(paper.equity));
      setText(B["paper.foot"], foot.join(" · ") || DASH);
    }

    if ($("#paperPosMeta")) setText($("#paperPosMeta"), posCount ? fmtInt(posCount) + " 只持仓" : "空仓");
    var sig = JSON.stringify(paper.positions);
    if (state.rendered.paper !== sig && L.positions) {
      state.rendered.paper = sig;
      var posTable = L.positions.closest(".tbl-wrap");
      if (!posCount) {
        L.positions.replaceChildren();
        show(posTable, false);
        show($("#posEmpty"), true);
      } else {
        show(posTable, true);
        show($("#posEmpty"), false);
        L.positions.innerHTML = paper.positions.map(function (p) {
          var toneCls = isNum(p.pnl) && p.pnl > 0 ? "is-pos" : isNum(p.pnl) && p.pnl < 0 ? "is-neg" : "";
          return "<tr><td><span class=\"mono\">" + esc(p.symbol) + "</span>" +
            (p.symbol && nameOf(p.symbol) ? '<span class="pos-name">' + esc(nameOf(p.symbol)) + "</span>" : "") +
            "</td>" +
            '<td class="num">' + esc(fmtInt(p.qty)) + "</td>" +
            '<td class="num">' + esc(fmtPrice(p.avgCost)) + "</td>" +
            '<td class="num">' + esc(fmtPrice(p.lastPrice)) + "</td>" +
            '<td class="num"><span class="' + toneCls + '">' + esc(fmtSignedMoney(p.pnl)) + "</span></td></tr>";
        }).join("");
      }
    } else if (!posCount && L.positions) {
      show($("#posEmpty"), true);
    }

    renderOrders(paper.orders);
  }

  var ORDER_STATUS = {
    filled: { cls: "tag", label: "已成交" },
    pending: { cls: "tag tag-flat", label: "待成交" },
    rejected: { cls: "tag tag-warn", label: "已拒绝" },
    expired: { cls: "tag tag-flat", label: "已过期" }
  };

  function orderTimeHtml(value) {
    var p = shParts(value);
    if (!p) return esc(DASH);
    return '<span class="t-date">' + p.m + "-" + p.d + "</span> " + p.hh + ":" + p.mm;
  }

  function renderOrders(orders) {
    var tbody = L.orders;
    var wrap = $("#ordersWrap");
    var empty = $("#ordersEmpty");
    var list = arr(orders).slice().sort(function (a, b) {
      return (Date.parse(b.createdAt) || 0) - (Date.parse(a.createdAt) || 0);
    }).slice(0, 12);
    if (!list.length) {
      if (tbody) tbody.replaceChildren();
      if (wrap) wrap.hidden = true;
      show(empty, true);
      renderInto(empty, state.dev && !IS_CLOUD ? {
        title: "静态预览 · 未连接数据服务",
        message: "纯静态预览没有订单记录。",
        command: "python -m quant.cli serve --port 8787",
        icon: "i-warning"
      } : IS_CLOUD ? {
        title: "云端不提供订单",
        message: "模拟成交记录只保存在本地 SQLite。",
        icon: "i-warning"
      } : {
        title: "暂无订单",
        message: "模拟盘推进后这里显示最近订单与拒单原因。",
        command: "python -m quant.cli paper --advance 1",
        icon: "i-warning"
      });
      if (B["orders.meta"]) setText(B["orders.meta"], "0 单");
      if (B["orders.foot"]) setText(B["orders.foot"], "暂无成交记录");
      return;
    }
    if (wrap) wrap.hidden = false;
    show(empty, false);
    var signature = list.map(function (o) {
      return [o.id, o.side, o.qty, o.status, o.filledPrice, o.createdAt].join("|");
    }).join(";");
    if (signature !== state.rendered.orders) {
      state.rendered.orders = signature;
      if (tbody) {
        tbody.innerHTML = list.map(function (o) {
          var side = o.side === "buy" ? { cls: "tag tag-up", label: "买入" }
            : o.side === "sell" ? { cls: "tag tag-down", label: "卖出" } : { cls: "tag", label: DASH };
          var st = ORDER_STATUS[o.status] || { cls: "tag", label: o.status || DASH };
          var title = o.rejectReason ? ' title="' + esc(o.rejectReason) + '"' : "";
          return "<tr><td><span class=\"t-time\">" + orderTimeHtml(o.createdAt) + "</span></td>" +
            '<td class="mono">' + esc(o.symbol) + "</td>" +
            '<td><span class="' + side.cls + '">' + esc(side.label) + "</span></td>" +
            '<td class="num">' + esc(fmtInt(o.qty)) + "</td>" +
            '<td class="num">' + esc(fmtPrice(o.targetPrice)) + "</td>" +
            '<td class="num">' + esc(fmtPrice(o.filledPrice)) + "</td>" +
            '<td class="num col-fee">' + esc(fmtMoney(o.fee)) + "</td>" +
            "<td" + title + '><span class="' + st.cls + '">' + esc(st.label) + "</span></td></tr>";
        }).join("");
      }
    }
    if (B["orders.meta"]) setText(B["orders.meta"], fmtInt(list.length) + " 单 · 最近 12 条");
    if (B["orders.foot"]) {
      var filled = list.filter(function (o) { return o.status === "filled"; }).length;
      setText(B["orders.foot"], "最近 " + fmtInt(list.length) + " 条 · 已成交 " + fmtInt(filled) + " 条");
    }
  }

  /* ============================== 渲染：净值曲线（SVG 手写） ============================== */
  function ensureEquityChart() {
    var wrap = $("#eqChart");
    if (!wrap || wrap.dataset.built === "1") return;
    wrap.dataset.built = "1";
    wrap.innerHTML =
      '<div class="eq-grid-layout">' +
      '<div class="eq-labels" id="eqYLabels"></div>' +
      '<div class="eq-plot" id="eqPlot">' +
      '<svg class="eq-svg" viewBox="0 0 1000 300" preserveAspectRatio="none" aria-hidden="true">' +
      "<defs><linearGradient id=\"eqFill\" x1=\"0\" y1=\"0\" x2=\"0\" y2=\"1\">" +
      '<stop id="eqFillTop" offset="0" stop-color="#4C8DFF" stop-opacity="0.14"/>' +
      '<stop offset="1" stop-color="#4C8DFF" stop-opacity="0"/>' +
      "</linearGradient></defs>" +
      '<g class="eq-grid" id="eqGrid"></g>' +
      '<line class="eq-base" id="eqBase" x1="0" y1="-10" x2="1000" y2="-10"/>' +
      '<path class="eq-area" id="eqArea" d="" fill="url(#eqFill)"/>' +
      '<path class="eq-line" id="eqLine" d=""/>' +
      "</svg>" +
      '<i class="eq-cross-x" id="eqCrossX"></i>' +
      '<i class="eq-cross-dot" id="eqCrossDot"></i>' +
      '<div class="eq-tip" id="eqTip"></div>' +
      "</div>" +
      '<div class="eq-x" id="eqXLabels"></div>' +
      "</div>";
    bindEquityHover($("#eqPlot"));
  }

  function bindEquityHover(plot) {
    if (!plot || plot.dataset.hover === "1") return;
    plot.dataset.hover = "1";
    var raf = 0;
    var pending = null;
    function apply() {
      raf = 0;
      if (!pending || !state.eqGeo || state.eqPoints.length < 2) return;
      var geo = state.eqGeo;
      var rect = plot.getBoundingClientRect();
      if (!rect.width) return;
      var frac = clamp((pending.clientX - rect.left) / rect.width, 0, 1);
      var idx = Math.round(frac * (state.eqPoints.length - 1));
      var pt = geo.points[idx];
      if (!pt) return;
      var px = (pt.x / 1000) * rect.width;
      var py = (pt.y / 300) * rect.height;
      var crossX = $("#eqCrossX");
      var crossDot = $("#eqCrossDot");
      var tip = $("#eqTip");
      if (crossX) crossX.style.transform = "translate3d(" + px.toFixed(1) + "px,0,0)";
      if (crossDot) crossDot.style.transform = "translate3d(" + px.toFixed(1) + "px," + py.toFixed(1) + "px,0)";
      if (tip) {
        var ret = geo.initial ? pt.p.equity / geo.initial - 1 : undefined;
        tip.innerHTML =
          '<div class="eq-tip-row"><span>' + esc(fmtDate(pt.p.ts)) + "</span><b>" + esc(fmtMoney(pt.p.equity)) + "</b></div>" +
          '<div class="eq-tip-row"><span>现金</span><b>' + esc(fmtMoney(pt.p.cash)) + "</b></div>" +
          '<div class="eq-tip-row"><span>收益</span><b' +
          (isNum(ret) ? ' style="color:' + (ret >= 0 ? "var(--up)" : "var(--down)") + '"' : "") + ">" +
          esc(fmtSignedPct(ret)) + "</b></div>";
        var tw = tip.offsetWidth || 120;
        var th = tip.offsetHeight || 52;
        var tx = px + 12;
        if (tx + tw > rect.width - 2) tx = px - tw - 12;
        var ty = clamp(py - th - 10, 2, Math.max(2, rect.height - th - 2));
        tip.style.transform = "translate3d(" + clamp(tx, 2, Math.max(2, rect.width - tw - 2)).toFixed(1) +
          "px," + ty.toFixed(1) + "px,0)";
        tip.style.opacity = "1";
      }
      if (crossX) crossX.style.opacity = "1";
      if (crossDot) crossDot.style.opacity = "1";
    }
    plot.addEventListener("pointermove", function (ev) {
      if (ev.pointerType === "touch") return;
      pending = { clientX: ev.clientX, clientY: ev.clientY };
      if (!raf) raf = requestAnimationFrame(apply);
    });
    plot.addEventListener("pointerleave", function () {
      pending = null;
      if (raf) { cancelAnimationFrame(raf); raf = 0; }
      ["#eqCrossX", "#eqCrossDot", "#eqTip"].forEach(function (sel) {
        var el = $(sel);
        if (el) el.style.opacity = "0";
      });
    });
  }

  function buildEquityGeometry(points) {
    var n = points.length;
    var values = points.map(function (p) { return p.equity; });
    var initial = state.paper && Number.isFinite(state.paper.initialCash) ? state.paper.initialCash : values[0];
    var lo = Math.min.apply(null, values.concat([initial]));
    var hi = Math.max.apply(null, values.concat([initial]));
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) return null;
    var span = hi - lo;
    if (span <= 0) span = Math.max(Math.abs(hi) * 0.01, 1);
    lo -= span * 0.08;
    hi += span * 0.08;
    var top = 8, bottom = 292;
    var mapY = function (v) { return bottom - ((v - lo) / (hi - lo)) * (bottom - top); };
    var pts = points.map(function (p, i) {
      return {
        x: n <= 1 ? 500 : (i / (n - 1)) * 1000,
        y: mapY(p.equity),
        p: p
      };
    });
    return { points: pts, lo: lo, hi: hi, top: top, bottom: bottom, initial: initial, mapY: mapY };
  }

  function renderEquity() {
    var wrap = $("#eqChart");
    if (!wrap) return;
    var points = state.equity;
    if (!points || points.length < 2) {
      if (state.rendered.equity === "empty") return;
      state.rendered.equity = "empty";
      wrap.dataset.built = "";
      var empty = state.dev && !IS_CLOUD ? {
        title: "静态预览 · 未连接数据服务",
        message: "启动本地数据服务并推进模拟盘后可查看净值曲线。",
        command: "python -m quant.cli serve --port 8787",
        icon: "i-chart"
      } : IS_CLOUD ? {
        title: "云端不提供净值曲线",
        message: "净值数据来自本地模拟盘。",
        icon: "i-chart"
      } : {
        title: "暂无净值曲线",
        message: "至少需要 2 个净值点才能画曲线。",
        command: "python -m quant.cli paper --init-cash 1000000 --advance 1",
        icon: "i-chart"
      };
      if (points && points.length === 1) {
        empty.message = "只有 1 个净值点，再推进一个交易日即可画曲线。";
        empty.command = "python -m quant.cli paper --advance 1";
      }
      wrap.innerHTML = '<div class="chart-empty"></div>';
      renderInto($(".chart-empty", wrap), empty);
      state.eqGeo = null;
      if (B["eq.meta"]) setText(B["eq.meta"], DASH);
      if (B["eq.foot"]) setText(B["eq.foot"], "暂无净值数据");
      if (B["eq.init"]) setText(B["eq.init"], "初始资金 " + DASH);
      return;
    }
    ensureEquityChart();
    var geo = buildEquityGeometry(points);
    if (!geo) return;
    state.eqGeo = geo;
    var signature = points.length + "|" + points[0].ts + "|" + points[points.length - 1].ts + "|" +
      points[points.length - 1].equity + "|" + points[0].equity;
    if (state.rendered.equity === signature) return;
    var firstDraw = state.rendered.equity === "" || state.rendered.equity === "empty";
    state.rendered.equity = signature;

    var lineD = geo.points.map(function (pt, i) {
      return (i === 0 ? "M" : "L") + pt.x.toFixed(2) + " " + pt.y.toFixed(2);
    }).join(" ");
    var areaD = lineD + " L1000 300 L0 300 Z";
    var line = $("#eqLine");
    var area = $("#eqArea");
    if (line) line.setAttribute("d", lineD);
    if (area) area.setAttribute("d", areaD);

    var startVal = points[0].equity;
    var endVal = points[points.length - 1].equity;
    var up = endVal >= startVal;
    var flatCurve = Math.abs(endVal - startVal) / (Math.abs(startVal) || 1) < 1e-6;
    var color = flatCurve ? "var(--text-1)" : up ? "var(--up)" : "var(--down)";
    if (line) line.style.stroke = color;
    var fillTop = $("#eqFillTop");
    if (fillTop) fillTop.setAttribute("stop-color", flatCurve ? "#8A94A6" : up ? "#FF4D4F" : "#22C55E");

    var ticks = [0, 1 / 3, 2 / 3, 1];
    var grid = $("#eqGrid");
    var labels = $("#eqYLabels");
    if (grid) grid.innerHTML = "";
    if (labels) labels.innerHTML = "";
    ticks.forEach(function (t) {
      var value = geo.hi - t * (geo.hi - geo.lo);
      var y = geo.top + t * (geo.bottom - geo.top);
      if (grid) {
        var g = document.createElementNS("http://www.w3.org/2000/svg", "line");
        g.setAttribute("class", "eq-grid");
        g.setAttribute("x1", "0"); g.setAttribute("x2", "1000");
        g.setAttribute("y1", y.toFixed(2)); g.setAttribute("y2", y.toFixed(2));
        grid.appendChild(g);
      }
      if (labels) {
        var lab = document.createElement("span");
        lab.className = "eq-y" + (t === 1 ? " is-base" : "");
        lab.style.top = ((y / 300) * 100) + "%";
        var smallRange = (geo.hi - geo.lo) / Math.max(Math.abs(geo.hi), 1) < 0.02;
        lab.textContent = value >= 100000
          ? (value / 10000).toFixed(smallRange ? 2 : 1) + "\u4E07"
          : value.toFixed(0);
        labels.appendChild(lab);
      }
    });

    var base = $("#eqBase");
    if (base) {
      if (Number.isFinite(geo.initial)) {
        var by = geo.mapY(geo.initial);
        base.setAttribute("y1", by.toFixed(2));
        base.setAttribute("y2", by.toFixed(2));
        base.style.display = "";
      } else {
        base.style.display = "none";
      }
    }

    var xLabels = $("#eqXLabels");
    if (xLabels) {
      var idxs = points.length > 2 ? [0, Math.floor((points.length - 1) / 2), points.length - 1] : [0, points.length - 1];
      xLabels.innerHTML = idxs.map(function (i) {
        return "<span>" + esc(fmtDateShort(points[i].ts)) + "</span>";
      }).join("");
    }

    if (firstDraw) {
      if (area) {
        area.style.opacity = "0";
        requestAnimationFrame(function () {
          area.style.transition = reduced() ? "none" : "opacity 400ms cubic-bezier(0.2,0,0,1)";
          area.style.opacity = flatCurve ? "0.55" : "1";
        });
      }
      if (line && line.getTotalLength) {
        var len = line.getTotalLength();
        if (len > 0) {
          line.style.strokeDasharray = len + "px";
          line.style.strokeDashoffset = reduced() ? "0px" : len + "px";
          if (!reduced() && line.animate) {
            line.animate([{ strokeDashoffset: len + "px" }, { strokeDashoffset: "0px" }],
              { duration: DRAW_MS, easing: "ease-out", fill: "forwards" });
          }
        }
      }
    } else if (line) {
      line.style.strokeDasharray = "none";
      line.style.strokeDashoffset = "0";
    }

    if (B["eq.meta"]) {
      B["eq.meta"].innerHTML = points.length + ' 点<span class="meta-range"> · ' +
        esc(rawDate(points[0].ts)) + " → " + esc(rawDate(points[points.length - 1].ts)) + "</span>";
    }
    if (B["eq.foot"]) {
      var ret = Number.isFinite(geo.initial) && geo.initial ? endVal / geo.initial - 1 : undefined;
      setText(B["eq.foot"], "最新 " + fmtMoney(endVal) + " · 相对于初始 " + fmtSignedPct(ret));
    }
    if (B["eq.init"]) setText(B["eq.init"], "初始资金 " + fmtMoney0(geo.initial));
    var plot = $("#eqPlot");
    if (plot) {
      plot.setAttribute("aria-label", "模拟盘净值曲线 " + points.length + " 个点，区间收益 " +
        fmtSignedPct(geo.initial ? endVal / geo.initial - 1 : undefined));
    }
  }

  /* ============================== 数据加载 ============================== */
  function noteChallenge() {
    state.gate.required = true;
    state.gate.blocked = true;
    if (!state.gate.passed) void openGate();
  }


  /* ============================== 市场整体面板 ============================== */
  function fmtNum(v, digits) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    var d = digits === undefined ? 2 : digits;
    return Number(v).toFixed(d);
  }
  function fmtInt(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return Number(v).toLocaleString("zh-CN");
  }
  function fmtClock(iso) {
    try {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return String(iso || "—").slice(11, 16) || "—";
      return d.toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" });
    } catch (e) { return "—"; }
  }
  function createMarketPanel() {
    var main = $("#main");
    if (!main || $("#marketCard")) return;
    var sec = document.createElement("section");
    sec.className = "card span-12";
    sec.id = "marketCard";
    sec.style.setProperty("--i", "0");
    sec.setAttribute("aria-labelledby", "mktTitle");
    sec.innerHTML =
      '<header class="card-head">' +
        '<div><p class="eyebrow">市场整体</p><h2 id="mktTitle">00 指数 · 情绪 · 冰点</h2></div>' +
        '<div class="card-meta"><span id="mktUpdated">更新 —</span><span class="dot" id="mktDot" aria-hidden="true"></span></div>' +
      '</header>' +
      '<div class="mkt-grid">' +
        '<div class="mkt-indices" id="mktIndices" aria-live="polite"><div class="mkt-empty">暂无市场数据</div></div>' +
        '<div class="mkt-sentiment">' +
          '<div class="mkt-row"><span class="muted">情绪温度</span><span><b class="num" id="mktScore">—</b><span class="pill" id="mktState">—</span></span></div>' +
          '<div class="sent-track"><i id="mktFill"></i><b id="mktMark"></b></div>' +
          '<div class="mkt-row muted"><span>涨跌家数</span><span id="mktBreadthText">—</span></div>' +
          '<div class="breadth-bar" id="mktBreadthBar"><i class="up"></i><i class="flat"></i><i class="down"></i></div>' +
          '<div class="mkt-row muted"><span>涨停 <b class="num up" id="mktLu">—</b></span><span>跌停 <b class="num down" id="mktLd">—</b></span><span>活跃度 <b class="num" id="mktActive">—</b></span></div>' +
        '</div>' +
        '<div class="mkt-dir">' +
          '<p class="muted">市场整体方向</p>' +
          '<div class="dir-pill" id="mktDir">—</div>' +
          '<p class="muted" id="mktDirMeta">—</p>' +
        '</div>' +
      '</div>';
    main.insertBefore(sec, main.firstChild);
  }
  function renderMarket(snapshot, updatedAt) {
    var box = $("#mktIndices");
    if (!box) return;
    if (!snapshot || !snapshot.indices || !snapshot.indices.length) {
      box.innerHTML = '<div class="mkt-empty">暂无市场数据</div>';
      setText($("#mktUpdated"), "更新 —");
      return;
    }
    var html = "";
    snapshot.indices.forEach(function (it) {
      var chg = Number(it.pct_chg || 0);
      var cls = chg > 0 ? "up" : (chg < 0 ? "down" : "flat");
      var dir = it.direction === "up" ? "偏多" : (it.direction === "down" ? "偏空" : "震荡");
      html += '<div class="idx-card">' +
        '<span class="idx-name">' + esc(it.name) + '</span>' +
        '<b class="idx-px num">' + fmtNum(it.price) + '</b>' +
        '<span class="idx-chg num ' + cls + '">' + (chg > 0 ? "+" : "") + fmtNum(chg) + '%</span>' +
        '<span class="idx-dir ' + cls + '">' + dir + ' ' + fmtNum((it.confidence || 0) * 100, 0) + '%</span>' +
      '</div>';
    });
    box.innerHTML = html;
    var sent = snapshot.sentiment || {};
    setText($("#mktScore"), fmtNum(sent.score, 1));
    var stateEl = $("#mktState");
    if (stateEl) {
      setText(stateEl, sent.label || "—");
      stateEl.className = "pill" + (sent.state === "ice" ? " ice" : (sent.state === "hot" ? " hot" : ""));
    }
    var pct = Math.max(0, Math.min(100, Number(sent.score || 0)));
    var fill = $("#mktFill"); if (fill) fill.style.width = pct + "%";
    var mark = $("#mktMark"); if (mark) mark.style.left = pct + "%";
    var act = snapshot.activity || {};
    setText($("#mktBreadthText"), "涨 " + fmtInt(act.up) + " / 跌 " + fmtInt(act.down));
    setText($("#mktLu"), fmtInt(act.limit_up));
    setText($("#mktLd"), fmtInt(act.limit_down));
    setText($("#mktActive"), fmtNum(act.active_pct) + "%");
    var bar = $("#mktBreadthBar");
    if (bar) {
      var up = Number(act.up || 0), down = Number(act.down || 0), flat = Number(act.flat || 0);
      var total = Math.max(1, up + down + flat);
      bar.children[0].style.width = (up / total * 100) + "%";
      bar.children[1].style.width = (flat / total * 100) + "%";
      bar.children[2].style.width = (down / total * 100) + "%";
    }
    var md = snapshot.market_direction || {};
    var dirEl = $("#mktDir");
    if (dirEl) {
      var ml = md.direction === "up" ? "偏多" : (md.direction === "down" ? "偏空" : "震荡");
      setText(dirEl, ml);
      dirEl.className = "dir-pill " + (md.direction === "up" ? "up" : (md.direction === "down" ? "down" : ""));
    }
    setText($("#mktDirMeta"), "置信度 " + fmtNum((md.confidence || 0) * 100, 1) + "%");
    setText($("#mktUpdated"), "更新 " + fmtClock(updatedAt || snapshot.asof) + " · 收盘数据");
  }
  function loadMarket() {
    return getJSON(EP.market).then(function (r) {
      if (r.challenge) { noteChallenge(); return { kind: "challenge" }; }
      if (r.rateLimited) { startRateLimit(r.retryAfter); return { kind: "ratelimit" }; }
      if (r.error) { return { kind: "error", error: r.error }; }
      state.market = r.data && r.data.snapshot ? r.data.snapshot : null;
      state.marketUpdatedAt = r.data && r.data.updated_at ? r.data.updated_at : null;
      renderMarket(state.market, state.marketUpdatedAt);
      return { kind: state.market ? "ok" : "empty" };
    });
  }

  function loadSignals() {
    return getJSON(EP.signals + "?limit=200").then(function (r) {
      if (r.challenge) { noteChallenge(); return { kind: "challenge" }; }
      if (r.rateLimited) {
        startRateLimit(r.retryAfter);
        if (!state.signals.length) { renderSignalCard(); renderSignalTable(); }
        return { kind: "ratelimit" };
      }
      if (r.error) {
        if (!state.signals.length) { renderSignalCard(); renderSignalTable(); }
        return { kind: "error", error: r.error };
      }
      var incoming = extractSignals(r.data).map(normSignal).filter(Boolean);
      /* 全量 GET：以服务端为准（空库时清空旧数据）；SSE 增量才做 merge */
      state.signals = incoming.length ? incoming : [];
      if (state.signals.length) state.lastAsof = state.signals[0].asof || state.lastAsof;
      else if (r.data && r.data.updated_at) state.lastAsof = String(r.data.updated_at);
      state.lastOkAt = Date.now();
      renderSignalCard();
      renderSignalTable();
      renderStatus();
      var latest = state.signals[0];
      if (latest && latest.id !== state.lastAnnouncedId) {
        state.lastAnnouncedId = latest.id;
        announce("信号更新：" + (latest.name || latest.symbol) + " " + dirView(latest).label);
      }
      return { kind: "ok" };
    });
  }

  function loadPaper() {
    return getJSON(EP.paper).then(function (r) {
      if (r.challenge) { noteChallenge(); return { kind: "challenge" }; }
      if (r.rateLimited) { startRateLimit(r.retryAfter); return { kind: "ratelimit" }; }
      if (r.error) {
        if (r.error.status === 404 || !state.paper) { state.paper = null; renderPaper(); return { kind: r.error.status === 404 ? "empty" : "error" }; }
        return { kind: "error", error: r.error };
      }
      state.paper = normPaper(r.data);
      state.lastOkAt = Date.now();
      renderPaper();
      renderStatus();
      return { kind: state.paper ? "ok" : "empty" };
    });
  }

  function loadEquity() {
    return getJSON(EP.equity).then(function (r) {
      if (r.challenge) { noteChallenge(); return { kind: "challenge" }; }
      if (r.rateLimited) { startRateLimit(r.retryAfter); return { kind: "ratelimit" }; }
      if (r.error) {
        if (r.error.status === 404 || !state.equity.length) { state.equity = []; renderEquity(); return { kind: r.error.status === 404 ? "empty" : "error" }; }
        return { kind: "error", error: r.error };
      }
      var points = normEquity(r.data);
      if (!points.length && state.paper && arr(r.data).length) points = normEquity(r.data);
      state.equity = points;
      renderEquity();
      return { kind: points.length ? "ok" : "empty" };
    });
  }

  function loadBacktest() {
    return getJSON(EP.backtest).then(function (r) {
      if (r.challenge) { noteChallenge(); return { kind: "challenge" }; }
      if (r.rateLimited) { startRateLimit(r.retryAfter); return { kind: "ratelimit" }; }
      if (r.error) {
        state.backtest = null;
        if (r.error.status === 404 && state.dev) {
          state.backtestError = null;
          renderBacktest();
          return { kind: "empty" };
        }
        if (r.error.status === 404) {
          var data = r.error.data || {};
          var msg = String(data.message || "尚未生成回测报告。");
          var command = "";
          var m = msg.match(/python\s+-m\s+quant\.cli\s+[\w-]+(?:\s+[^\s。]+)*/);
          if (m) { command = m[0]; msg = msg.replace(m[0], "").replace(/^[：:，,\s]+/, "").replace(/[：:，,\s]+$/, ""); }
          state.backtestError = {
            title: "未回测",
            message: msg || "尚未生成回测报告。",
            command: command || "python -m quant.cli backtest",
            note: "报告写入 data/reports/backtest.json，刷新后展示指标与校准图。",
            icon: "i-chart"
          };
          renderBacktest();
          return { kind: "empty" };
        }
        state.backtestError = {
          title: "回测接口不可用",
          message: r.error.message || "无法读取回测报告。",
          note: "保留其它区块，下次轮询自动重试。",
          icon: "i-warning"
        };
        renderBacktest();
        return { kind: "error", error: r.error };
      }
      state.backtest = normBacktest(r.data);
      renderBacktest();
      return { kind: state.backtest ? "ok" : "empty" };
    });
  }

  function refreshAll(reason) {
    if (state.rateLimitUntil > Date.now()) return Promise.resolve();
    if (state.gate.blocked) return Promise.resolve();
    if (state.inflight) { state.refreshQueued = true; return Promise.resolve(); }
    state.inflight = true;
    var btn = $("#refreshBtn");
    if (btn) btn.classList.add("is-loading");
    return Promise.all([loadSignals(), loadMarket(), loadPaper(), loadEquity(), loadBacktest()]).then(function (results) {
      state.inflight = false;
      if (btn) btn.classList.remove("is-loading");
      evaluateConnection(results);
      if (state.refreshQueued) {
        state.refreshQueued = false;
        setTimeout(function () { void refreshAll("queued"); }, 0);
      }
    }, function () {
      state.inflight = false;
      if (btn) btn.classList.remove("is-loading");
    });
  }

  function evaluateConnection(results) {
    var kinds = results.map(function (r) { return r && r.kind; });
    if (kinds.indexOf("challenge") !== -1) return;
    if (kinds.indexOf("ratelimit") !== -1) return;
    var ok = kinds.filter(function (k) { return k === "ok" || k === "empty"; }).length;
    var errors = kinds.filter(function (k) { return k === "error"; }).length;
    if (kinds[0] === "ok") setConn(errors > 0 ? "degraded" : "live");
    else if (ok > 0) setConn("degraded");
    else setConn("offline");
    renderStatus();
  }

  /* ============================== 轮询 / SSE ============================== */
  function startPolling() {
    if (IS_CLOUD) return;
    stopPolling();
    state.pollTimer = setInterval(function () {
      if (state.hidden) return;
      if (state.rateLimitUntil > Date.now()) return;
      if (state.gate.blocked) return;
      void refreshAll("poll");
    }, POLL_MS);
  }
  function stopPolling() {
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = null;
  }

  function handleStreamPayload(dataText) {
    if (!dataText) return;
    var payload;
    try { payload = JSON.parse(dataText); } catch (e) { return; }
    var incoming = extractSignals(payload).map(normSignal).filter(Boolean);
    if (!incoming.length && payload && payload.updated_at) {
      state.lastAsof = String(payload.updated_at);
      return;
    }
    state.signals = mergeSignals(incoming);
    if (state.signals.length) state.lastAsof = state.signals[0].asof || state.lastAsof;
    state.lastOkAt = Date.now();
    state.streamBackoff = 1000;
    setConn("live");
    renderSignalCard();
    renderSignalTable();
    renderStatus();
    var latest = state.signals[0];
    if (latest && latest.id !== state.lastAnnouncedId) {
      state.lastAnnouncedId = latest.id;
      announce("新信号：" + (latest.name || latest.symbol) + " " + dirView(latest).label);
    }
  }

  function pauseStream() {
    state.streamPaused = true;
    if (state.streamTimer) { clearTimeout(state.streamTimer); state.streamTimer = null; }
    if (state.stream) { state.stream.close(); state.stream = null; }
  }
  function resumeStream() {
    state.streamPaused = false;
    if (IS_CLOUD) { startStream(); }
  }

  function scheduleStreamRetry(reason) {
    if (!IS_CLOUD || state.streamPaused || state.hidden) return;
    if (state.streamTimer) clearTimeout(state.streamTimer);
    var delay = Math.round(state.streamBackoff * (0.9 + Math.random() * 0.2));
    state.streamBackoff = Math.min(state.streamBackoff * 2, 8000);
    setConn(state.streamBackoff >= 8000 ? "offline" : "reconnecting");
    state.streamTimer = setTimeout(function () {
      state.streamTimer = null;
      startStream(reason);
    }, delay);
  }

  function startStream() {
    if (!IS_CLOUD || state.stream || state.streamPaused || state.hidden) return;
    if (state.gate.blocked) return;
    if (state.rateLimitUntil > Date.now()) return;
    var es = new EventSource(EP.stream);
    state.stream = es;
    es.onopen = function () {
      state.streamBackoff = 1000;
      setConn("live");
    };
    es.addEventListener("hello", function () { setConn("live"); });
    es.addEventListener("signals", function (ev) { handleStreamPayload(ev.data); });
    es.addEventListener("heartbeat", function () { setConn("live"); });
    es.addEventListener("bye", function () {
      if (state.stream) { state.stream.close(); state.stream = null; }
      scheduleStreamRetry("bye");
    });
    es.addEventListener("error", function (ev) {
      if (!ev.data) return;
      try {
        var payload = JSON.parse(ev.data);
        if (payload && payload.error === "rate_limited") {
          startRateLimit(num(payload.retry_after) || 30);
        }
      } catch (e) { /* ignore */ }
    });
    es.onerror = function () {
      if (state.stream) { state.stream.close(); state.stream = null; }
      if (state.streamPaused) return;
      /* 401（未验证）时 EventSource 只会报错：用一次普通请求探测挑战 */
      if (!state.gate.passed) {
        void getJSON(EP.signals + "?limit=1").then(function (r) {
          if (r.challenge) { noteChallenge(); return; }
          scheduleStreamRetry("error");
        });
      } else {
        scheduleStreamRetry("error");
      }
    };
  }

  function onVisibility() {
    state.hidden = document.hidden;
    if (document.hidden) {
      stopPolling();
      pauseStream();
      return;
    }
    if (state.gate.blocked) return;
    if (state.rateLimitUntil <= Date.now()) void refreshAll("visible");
    if (IS_CLOUD) resumeStream();
    else startPolling();
  }

  /* ============================== 骨架屏 ============================== */
  function skBlock(lines) {
    var html = '<div class="sk-rows">';
    for (var i = 0; i < lines; i += 1) {
      html += '<div class="sk-row"><span class="sk sk-line"></span>' +
        '<span class="sk sk-line' + (i % 2 ? " w60" : "") + '"></span>' +
        '<span class="sk sk-line w40"></span></div>';
    }
    return html + "</div>";
  }

  function bootSkeletons() {
    if (L.features) L.features.innerHTML = skBlock(5);
    var eq = $("#eqChart");
    if (eq) {
      eq.innerHTML = '<div class="eq-grid-layout"><div class="eq-labels"></div><div class="eq-plot"><span class="sk sk-chart"></span></div><div class="eq-x"></div></div>';
    }
    if (L.dirs) L.dirs.innerHTML = [0, 1, 2].map(function () {
      return '<div class="dir-row"><span class="sk sk-line w60"></span><span class="sk sk-line"></span>' +
        '<span class="sk sk-line"></span><span class="sk sk-line"></span></div>';
    }).join("");
    if (L.benchmarks) {
      L.benchmarks.innerHTML = [0, 1, 2, 3].map(function () {
        return '<tr><td><span class="sk sk-line"></span></td><td><span class="sk sk-line"></span></td>' +
          '<td><span class="sk sk-line"></span></td><td><span class="sk sk-line"></span></td>' +
          '<td><span class="sk sk-line"></span></td></tr>';
      }).join("");
    }
    if (L.positions) {
      L.positions.innerHTML = [0, 1].map(function () {
        return '<tr><td><span class="sk sk-line"></span></td><td><span class="sk sk-line"></span></td>' +
          '<td><span class="sk sk-line"></span></td><td><span class="sk sk-line"></span></td>' +
          '<td><span class="sk sk-line"></span></td></tr>';
      }).join("");
    }
    if (L.orders) {
      L.orders.innerHTML = [0, 1, 2].map(function () {
        return '<tr><td><span class="sk sk-line"></span></td><td><span class="sk sk-line"></span></td>' +
          '<td><span class="sk sk-line"></span></td><td><span class="sk sk-line"></span></td>' +
          '<td><span class="sk sk-line"></span></td><td><span class="sk sk-line"></span></td>' +
          '<td><span class="sk sk-line"></span></td><td><span class="sk sk-line"></span></td></tr>';
      }).join("");
    }
    if (L.signals) {
      L.signals.innerHTML = [0, 1, 2, 3, 4].map(function () {
        return '<tr><td><span class="sk sk-line"></span></td><td><span class="sk sk-line"></span></td>' +
          '<td><span class="sk sk-line"></span></td><td><span class="sk sk-line"></span></td>' +
          '<td><span class="sk sk-line"></span></td><td><span class="sk sk-line"></span></td>' +
          '<td><span class="sk sk-line"></span></td><td><span class="sk sk-line"></span></td></tr>';
      }).join("");
    }
    var cal = $("#calChart");
    if (cal) cal.innerHTML = '<div class="sk" style="position:absolute;inset:0"></div>';
  }

  /* ============================== 免责声明 / 事件 ============================== */
  function bindDialog() {
    var dlg = $("#disclaimerDialog");
    if (!dlg) return;
    function open() {
      if (typeof dlg.showModal === "function") dlg.showModal();
      else dlg.setAttribute("open", "");
    }
    function close() {
      if (typeof dlg.close === "function") dlg.close();
      else dlg.removeAttribute("open");
    }
    ["#disclaimerBtn", "#footDisclaimer"].forEach(function (sel) {
      var btn = $(sel);
      if (btn) btn.addEventListener("click", open);
    });
    ["#dlgClose", "#dlgOk"].forEach(function (sel) {
      var btn = $(sel);
      if (btn) btn.addEventListener("click", close);
    });
    dlg.addEventListener("click", function (ev) {
      if (ev.target === dlg) close();
    });
  }

  function bindTabsKeyboard() {
    var host = $("#sigTabs");
    if (!host) return;
    host.addEventListener("keydown", function (ev) {
      if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
      var tabs = $$(".sym-tab", host);
      if (!tabs.length) return;
      var idx = tabs.findIndex(function (t) { return t.getAttribute("aria-selected") === "true"; });
      var next = ev.key === "ArrowRight" ? (idx + 1) % tabs.length : (idx - 1 + tabs.length) % tabs.length;
      tabs[next].focus();
      tabs[next].click();
      ev.preventDefault();
    });
  }

  function bindEvents() {
    var refresh = $("#refreshBtn");
    if (refresh) refresh.addEventListener("click", function () {
      if (state.gate.blocked) return;
      void refreshAll("manual");
    });
    var noticeRetry = $("#noticeRetry");
    if (noticeRetry) noticeRetry.addEventListener("click", clearRateLimit);
    var gateRetry = $("#gateRetry");
    if (gateRetry) gateRetry.addEventListener("click", retryGate);
    document.addEventListener("visibilitychange", onVisibility);
    if (motionQuery && motionQuery.addEventListener) {
      motionQuery.addEventListener("change", function () { /* 动画分支运行时读取 reduced() */ });
    }
    window.addEventListener("online", function () {
      if (!state.gate.active) void refreshAll("online");
    });
    window.addEventListener("offline", function () {
      setConn("offline");
    });
  }

  /* ============================== 启动 ============================== */
  function init() {
    collectDom();
    createMarketPanel();
    bindEvents();
    bindDialog();
    bindTabsKeyboard();
    bootSkeletons();
    updateSession();
    setInterval(updateSession, 30000);
    setConn("connecting");
    if (B["signals.mode"]) setText(B["signals.mode"], IS_CLOUD ? "SSE · 实时" : "60s 轮询");
    if (B["foot.mode"]) setText(B["foot.mode"], IS_CLOUD ? "CLOUDFLARE PAGES" : "LOCAL");
    if (B["foot.gate"]) setText(B["foot.gate"], "人机验证检测中");

    void detectGate().then(function () {
      if (!state.dev) return;
      renderSignalCard();
      renderSignalTable();
      renderPaper();
      renderEquity();
      renderBacktest();
    });
    void refreshAll("boot").then(function () {
      if (IS_CLOUD) startStream();
      else startPolling();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
