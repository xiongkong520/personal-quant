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
    market: "/api/market",
    stock: "/api/stock",
    events: "/api/events",
    kline: "/api/kline",
    regime: "/api/regime",
    intraday: "/api/intraday",
    marketHistory: "/api/market/history",
    summary: "/api/summary/latest",
    search: "/api/search"
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
    regime: null,
    regimeUpdatedAt: null,
    kline: {
      symbol: "",
      name: "",
      items: [],
      cache: new Map(),
      view: { start: 0, count: 0 },
      hover: -1,
      pointer: null,
      req: 0,
      raf: 0,
      loading: false,
      auto: true
    },
    intraday: { code: "sh000001", data: null, cache: new Map(), req: 0, geo: null },
    marketHistory: null,
    summary: null,
    prevIndexPrices: {},
    marketPollTimer: null,
    clockTimer: null,
    search: null,
    todayBootstrapped: false,
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
        note: "数据服务连接后自动刷新。",
        icon: "i-warning"
      };
    }
    if (IS_CLOUD) {
      return {
        title: "等待云端信号",
        message: "暂无云端信号数据。",
        command: "python -m quant.cli export --target cloudflare",
        note: "数据更新后自动刷新。",
        icon: "i-warning"
      };
    }
    return {
      title: "暂无信号",
      message: "暂无信号数据。",
      command: "python -m quant.cli signal",
      note: "页面每 60 秒自动刷新。",
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
    tr.dataset.symbol = sig.symbol;
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
    tr.dataset.symbol = sig.symbol;
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
          title: "回测数据暂未同步",
          message: "云端回测报告同步后会在此展示。",
          note: "无需操作，自动刷新。",
          icon: "i-chart"
        };
      } else if (!conf.message && state.dev) {
        conf = {
          title: "静态预览 · 未连接数据服务",
          message: "启动本地数据服务后可查看回测报告。",
          command: "python -m quant.cli serve --port 8787",
          note: "该区域数据暂未生成。",
          icon: "i-chart"
        };
      } else if (!conf.message) {
        conf = {
          title: "未回测",
          message: "尚未生成回测报告。",
          command: "python -m quant.cli backtest --start 2025-01-01 --end 2026-11-20",
          note: "回测数据生成后自动展示指标与校准图。",
          icon: "i-chart"
        };
      }
      renderInto(empty, conf);
      if (B["bt.meta"]) setText(B["bt.meta"], DASH);
      if (B["bt.range"]) setText(B["bt.range"], DASH);
      if (B["bt.foot"]) setText(B["bt.foot"], "回测报告不可用");
      var extras = $("#btExtras");
      if (extras) extras.hidden = true;
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

    renderBacktestExtras(bt);

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
        note: "本地研究环境提供。",
        icon: "i-warning"
      } : IS_CLOUD ? {
        title: "模拟盘数据暂未同步",
        message: "云端模拟盘快照同步后会在此展示。",
        note: "无需操作，自动刷新。",
        icon: "i-warning"
      } : {
        title: "模拟盘尚无快照",
        message: "初始化账户并推进一天后，这里显示现金、持仓与胜率。",
        command: "python -m quant.cli paper --init-cash 1000000 --advance 1",
        note: "数据更新后 60 秒内自动刷新。",
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
        title: "订单数据暂未同步",
        message: "模拟成交记录同步后会在此展示。",
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
        title: "净值数据暂未同步",
        message: "模拟盘净值同步后会在此展示。",
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
        '<div><p class="eyebrow">市场整体</p><h2 id="mktTitle">00 指数 · 情绪 · 温度</h2></div>' +
        '<div class="card-meta"><span id="mktUpdated">更新 —</span><span class="dot" id="mktDot" aria-hidden="true"></span></div>' +
      '</header>' +
      '<div class="mkt-grid">' +
        '<div class="mkt-indices" id="mktIndices" aria-live="polite"><div class="mkt-empty">暂无市场数据</div></div>' +
        '<div class="mkt-sentiment">' +
          '<div class="mkt-row"><span class="muted">情绪温度</span><span><b class="num" id="mktScore">—</b><span class="pill" id="mktState">—</span></span></div>' +
          '<div class="mkt-row mkt-split muted"><span>赚钱效应 <b class="num" id="mktBreadthScore">—</b></span><span>投机热度 <b class="num" id="mktHeatScore">—</b></span></div>' +
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
    ensurePanoramaSections();
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
      var pillCls = "pill";
      if (sent.state === "ice") pillCls += " ice";
      else if (sent.state === "hot") pillCls += " hot";
      else if (sent.state === "divergence") pillCls += " divergence";
      else if (sent.state === "cold") pillCls += " cold";
      stateEl.className = pillCls;
      stateEl.title = sent.state === "divergence" ? "涨跌家数偏弱但涨停结构强，赚钱效应与投机热度背离" : "";
    }
    setText($("#mktBreadthScore"), Number.isFinite(Number(sent.breadth_score)) ? fmtNum(Number(sent.breadth_score), 1) + "%" : "—");
    setText($("#mktHeatScore"), Number.isFinite(Number(sent.heat_score)) ? fmtNum(Number(sent.heat_score), 1) + "%" : "—");
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
    renderPanorama();
    renderRealtimeStrip();
    var live = Boolean(snapshot.live) || isTradingSession();
    setText($("#mktUpdated"), (live ? "实时 " : "收盘数据 ") + fmtClock(updatedAt || snapshot.asof));
  }
  function loadMarket(options) {
    var opts = options || {};
    return getJSON(EP.market).then(function (r) {
      if (r.challenge) { noteChallenge(); return { kind: "challenge" }; }
      if (r.rateLimited) { startRateLimit(r.retryAfter); return { kind: "ratelimit" }; }
      if (r.error) { return { kind: "error", error: r.error }; }
      state.market = r.data && r.data.snapshot ? r.data.snapshot : null;
      state.marketUpdatedAt = r.data && r.data.updated_at ? r.data.updated_at : null;
      renderMarket(state.market, state.marketUpdatedAt);
      if (!state.todayBootstrapped) {
        state.todayBootstrapped = true;
        void loadIntraday(state.intraday.code, { force: true });
        void loadMarketHistory();
      }
      return { kind: state.market ? "ok" : "empty" };
    });
  }


  /* ============================== 股票代码查询 ============================== */
  function ensureStockCard() {
    var main = $("#main");
    if (!main) return null;
    var card = $("#stockResult");
    if (card) return card;
    card = document.createElement("section");
    card.className = "card span-12";
    card.id = "stockResult";
    card.hidden = true;
    card.innerHTML = '<header class="card-head"><div><p class="eyebrow">代码查询</p><h2 id="stockTitle">个股快照</h2></div>' +
      '<div class="card-meta"><span id="stockMeta">—</span></div></header><div id="stockBody" class="stock-body"></div>';
    main.insertBefore(card, main.firstChild);
    return card;
  }
  function showStockMessage(text) {
    var card = ensureStockCard();
    if (!card) return;
    card.hidden = false;
    var body = $("#stockBody");
    if (body) body.innerHTML = '<p class="muted">' + esc(text) + "</p>";
    var title = $("#stockTitle");
    if (title) setText(title, "个股快照");
    var meta = $("#stockMeta");
    if (meta) setText(meta, "—");
  }
  function renderStockResult(data) {
    var card = ensureStockCard();
    if (!card) return;
    card.hidden = false;
    var sig = data.signal || {};
    var prob = sig.prob || {};
    var dir = sig.direction === "up" ? "看涨" : (sig.direction === "down" ? "看跌" : "震荡");
    var cls = sig.direction === "up" ? "up" : (sig.direction === "down" ? "down" : "flat");
    var name = data.name || sig.name || "";
    var title = $("#stockTitle");
    if (title) setText(title, (name ? name + " " : "") + (data.symbol || sig.symbol || ""));
    var meta = $("#stockMeta");
    if (meta) setText(meta, (sig.asof ? String(sig.asof).slice(0, 16).replace("T", " ") : "—") + " · " + (sig.status === "ok" ? "有效" : (sig.status || "—")));
    var pct = function (v) { return fmtNum((v || 0) * 100, 1) + "%"; };
    var body = $("#stockBody");
    if (!body) return;
    body.innerHTML =
      '<div class="stock-grid">' +
        '<div class="stock-main">' +
          '<span class="stock-dir ' + cls + '">' + dir + "</span>" +
          '<b class="stock-price num">' + fmtNum(sig.price) + "</b>" +
          '<span class="muted">置信度 ' + pct(sig.confidence) + "</span>" +
        "</div>" +
        '<div class="stock-probs">' +
          '<div class="prob-row"><span>看涨</span><b class="num up">' + pct(prob.up) + "</b></div>" +
          '<div class="prob-row"><span>震荡</span><b class="num">' + pct(prob.flat) + "</b></div>" +
          '<div class="prob-row"><span>看跌</span><b class="num down">' + pct(prob.down) + "</b></div>" +
        "</div>" +
        '<div class="stock-meta">' +
          '<span>预测周期 ' + esc(sig.horizon || "1d") + "</span>" +
          '<span>有效期至 ' + esc(String(sig.expires_at || "—").slice(0, 16).replace("T", " ")) + "</span>" +
        "</div>" +
      "</div>";
  }
  /* ============================== 顶部指数条 ============================== */
  /* ============================== 实时行情条（顶部固定） ============================== */
  var INDEX_TABS = [
    { code: "sh000001", name: "上证指数" },
    { code: "sz399001", name: "深证成指" },
    { code: "sz399006", name: "创业板指" },
    { code: "sh000300", name: "沪深300" }
  ];

  function isTradingSession() {
    var p = shParts(new Date());
    if (!p) return false;
    var wd = p.wd;
    if (wd === "Sat" || wd === "Sun") return false;
    var mins = Number(p.hh) * 60 + Number(p.mm);
    return (mins >= 9 * 60 + 30 && mins <= 11 * 60 + 30) || (mins >= 13 * 60 && mins <= 15 * 60);
  }

  function fmtAmountShort(value) {
    if (!isNum(value)) return "";
    if (value >= 1e12) return (value / 1e12).toFixed(2) + "万亿";
    if (value >= 1e8) return (value / 1e8).toFixed(0) + "亿";
    if (value >= 1e4) return (value / 1e4).toFixed(0) + "万";
    return String(Math.round(value));
  }

  function flashValue(el, dir) {
    if (!el || reduced() || typeof el.animate !== "function") return;
    var color = dir === "down" ? "var(--down)" : "var(--up)";
    try {
      el.animate(
        [{ backgroundColor: "rgba(76,141,255,0)" }, { backgroundColor: dir ? "rgba(255,255,255,0.08)" : "rgba(76,141,255,0.1)" }, { backgroundColor: "rgba(76,141,255,0)" }],
        { duration: 320, easing: "ease-out" }
      );
      el.style.color = color;
      setTimeout(function () { el.style.color = ""; }, 340);
    } catch (e) { /* 忽略 */ }
  }

  function createIndexStrip() {
    if ($("#indexStrip")) return;
    var header = document.querySelector("header.topbar") || document.querySelector("header");
    if (!header || !header.parentNode) return;
    var strip = document.createElement("div");
    strip.id = "indexStrip";
    strip.className = "rt-strip";
    strip.hidden = true;
    strip.innerHTML =
      '<div class="rt-inner">' +
        '<div class="rt-indices" id="rtIndices"></div>' +
        '<div class="rt-sent">' +
          '<span class="rt-label">情绪</span>' +
          '<b class="num" id="rtScore">—</b>' +
          '<span class="pill" id="rtPill">—</span>' +
          '<span class="rt-zone" aria-hidden="true"><i id="rtFill"></i><b id="rtMark"></b></span>' +
        '</div>' +
        '<div class="rt-breadth">' +
          '<div class="rt-breadth-num"><span class="up" id="rtUp">—</span><span class="rt-sep">/</span><span class="down" id="rtDown">—</span>' +
            '<span class="rt-flat" id="rtFlat">—</span></div>' +
          '<div class="rt-dist" aria-hidden="true"><i class="up" id="rtDistUp"></i><i class="flat" id="rtDistFlat"></i><i class="down" id="rtDistDown"></i></div>' +
          '<div class="rt-extra"><span>涨停 <b class="num up" id="rtLu">—</b></span><span>跌停 <b class="num down" id="rtLd">—</b></span><span>活跃 <b class="num" id="rtActive">—</b></span></div>' +
        '</div>' +
        '<div class="rt-right"><span class="rt-live" id="rtLive" data-live="0"><i class="dot"></i><span id="rtLiveText">实时 · --:--:--</span></span></div>' +
      '</div>';
    header.parentNode.insertBefore(strip, header.nextSibling);
  }

  function renderRealtimeStrip() {
    var strip = $("#indexStrip");
    if (!strip) return;
    var market = state.market;
    var indices = (market && market.indices) || [];
    if (!indices.length) {
      strip.hidden = true;
      return;
    }
    strip.hidden = false;
    var host = $("#rtIndices");
    if (host) {
      var prev = state.prevIndexPrices || {};
      var next = {};
      host.innerHTML = indices.slice(0, 4).map(function (it) {
        var chg = num(it.pct_chg);
        var cls = isNum(chg) && chg > 0 ? "up" : isNum(chg) && chg < 0 ? "down" : "flat";
        var amount = isNum(it.amount) ? '<span class="rt-amt num">' + fmtAmountShort(it.amount) + "</span>" : "";
        next[it.code] = it.price;
        return '<div class="rt-idx" data-code="' + esc(it.code) + '">' +
          '<span class="rt-idx-name">' + esc(it.name || it.code) + "</span>" +
          '<b class="rt-idx-price num ' + cls + '" data-price="' + esc(it.code) + '">' + fmtNum(it.price) + "</b>" +
          '<span class="rt-idx-chg num ' + cls + '">' + (isNum(chg) && chg > 0 ? "+" : "") + fmtNum(chg) + "%</span>" +
          amount +
          "</div>";
      }).join("");
      $$("[data-price]", host).forEach(function (el) {
        var code = el.dataset.price;
        if (isNum(prev[code]) && isNum(next[code]) && prev[code] !== next[code]) {
          flashValue(el, next[code] > prev[code] ? "up" : next[code] < prev[code] ? "down" : "");
        }
      });
      state.prevIndexPrices = next;
    }
    var sent = (market && market.sentiment) || {};
    var score = num(sent.score);
    setText($("#rtScore"), isNum(score) ? fmtNum(score, 1) : DASH);
    var pill = $("#rtPill");
    if (pill) {
      pill.textContent = sent.label || "—";
      pill.className = "pill" + (sent.state === "ice" ? " ice" : sent.state === "hot" || sent.state === "warm" ? " hot" : "");
    }
    var fill = $("#rtFill");
    var mark = $("#rtMark");
    var pct = clamp(isNum(score) ? score : 0, 0, 100);
    if (fill) fill.style.width = pct + "%";
    if (mark) mark.style.left = pct + "%";
    var act = (market && market.activity) || {};
    var up = num(act.up) || 0;
    var down = num(act.down) || 0;
    var flat = num(act.flat) || 0;
    var total = Math.max(1, up + down + flat);
    setText($("#rtUp"), fmtInt(up));
    setText($("#rtDown"), fmtInt(down));
    setText($("#rtFlat"), "平 " + fmtInt(flat));
    setText($("#rtLu"), fmtInt(act.limit_up));
    setText($("#rtLd"), fmtInt(act.limit_down));
    setText($("#rtActive"), isNum(act.active_pct) ? fmtNum(act.active_pct) + "%" : DASH);
    if ($("#rtDistUp")) $("#rtDistUp").style.width = (up / total) * 100 + "%";
    if ($("#rtDistFlat")) $("#rtDistFlat").style.width = (flat / total) * 100 + "%";
    if ($("#rtDistDown")) $("#rtDistDown").style.width = (down / total) * 100 + "%";
    var live = Boolean(market && market.live) || isTradingSession();
    var liveEl = $("#rtLive");
    if (liveEl) liveEl.dataset.live = live ? "1" : "0";
    updateRealtimeClock();
  }

  function updateRealtimeClock() {
    var text = $("#rtLiveText");
    if (!text) return;
    var p = shParts(new Date());
    var market = state.market;
    var live = Boolean(market && market.live) || isTradingSession();
    if (live) {
      text.textContent = "实时 · " + (p ? p.hh + ":" + p.mm + ":" + p.ss : "--:--:--");
    } else {
      var stamp = (market && market.trade_date) || (state.marketUpdatedAt ? String(state.marketUpdatedAt).slice(0, 10) : "");
      text.textContent = "收盘数据" + (stamp ? " · " + stamp : "");
    }
    var liveEl = $("#rtLive");
    if (liveEl) liveEl.dataset.live = live ? "1" : "0";
  }

  /* ============================== 今日走势（指数分时 + 情绪曲线） ============================== */
  function todayIndexName(code) {
    for (var i = 0; i < INDEX_TABS.length; i += 1) {
      if (INDEX_TABS[i].code === code) return INDEX_TABS[i].name;
    }
    return code;
  }

  function fmtIntradayTime(value) {
    var s = String(value || "");
    var m = s.match(/(\d{1,2}:\d{2})/);
    return m ? m[1] : s.slice(-5);
  }

  function normIntradayPayload(payload) {
    var data = payload && payload.data && typeof payload.data === "object" ? payload.data : payload;
    if (!data || typeof data !== "object") return null;
    var raw = Array.isArray(data) ? data : (data.points || data.items || data.series || []);
    if (!Array.isArray(raw) || !raw.length) return null;
    var points = [];
    raw.forEach(function (pt) {
      if (!pt || typeof pt !== "object") return;
      var ts = fmtIntradayTime(pt.ts || pt.time || pt.t);
      var price = num(pt.price ?? pt.close ?? pt.p);
      if (!ts || !isNum(price)) return;
      points.push({
        ts: ts,
        price: price,
        avg: num(pt.avg_price ?? pt.avg ?? pt.average),
        volume: num(pt.volume ?? pt.vol) || 0
      });
    });
    if (!points.length) return null;
    points.sort(function (a, b) { return a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : 0; });
    return {
      code: data.code || (payload && payload.code) || "",
      name: data.name || (payload && payload.name) || "",
      tradeDate: data.trade_date || (payload && payload.trade_date) || "",
      preClose: num(data.pre_close ?? data.prev_close ?? (payload && payload.pre_close)),
      points: points
    };
  }

  function createTodayPanel() {
    var main = $("#main");
    if (!main || $("#todayCard")) return;
    var tabs = INDEX_TABS.map(function (it, i) {
      return '<button type="button" class="td-tab" role="tab" data-code="' + it.code + '" aria-selected="' + (i === 0 ? "true" : "false") + '">' + it.name + "</button>";
    }).join("");
    var sec = document.createElement("section");
    sec.id = "todayCard";
    sec.className = "card span-8 today-card";
    sec.setAttribute("aria-labelledby", "tdTitle");
    sec.innerHTML =
      '<header class="card-head">' +
        '<h2 class="card-title" id="tdTitle">今日走势<span class="card-sub">指数分时 · 情绪曲线</span></h2>' +
        '<div class="card-meta"><span id="tdStatus">—</span></div>' +
      '</header>' +
      '<div class="today-tabs" id="tdTabs" role="tablist" aria-label="指数切换">' + tabs +
        '<span class="today-tab-meta num" id="tdMeta">—</span>' +
      '</div>' +
      '<div class="today-body">' +
        '<div class="td-main">' +
          '<div class="td-main-head"><span class="num" id="tdPrice">—</span><span class="num" id="tdChg">—</span><span class="muted" id="tdDate">—</span></div>' +
          '<div class="td-canvas-wrap" id="tdWrap">' +
            '<canvas id="tdCanvas" class="td-canvas" role="img" aria-label="指数分时图"></canvas>' +
            '<div class="td-empty" id="tdEmpty" hidden></div>' +
            '<div class="chart-tip td-tip" id="tdTip" hidden></div>' +
          '</div>' +
        '</div>' +
        '<div class="td-side">' +
          '<div class="td-side-head"><span>情绪 · 涨跌家数</span><span class="col-meta num" id="tdSideMeta">—</span></div>' +
          '<div class="td-side-canvas-wrap">' +
            '<canvas id="tdSideCanvas" class="td-side-canvas" role="img" aria-label="当日情绪与涨跌家数曲线"></canvas>' +
            '<div class="td-empty" id="tdSideEmpty" hidden></div>' +
          '</div>' +
          '<div class="td-side-foot"><span><i class="lg-line-dot accent"></i>情绪分</span><span><i class="lg-line-dot up"></i>上涨</span><span><i class="lg-line-dot down"></i>下跌</span></div>' +
        '</div>' +
      '</div>' +
      '<footer class="card-foot"><span id="tdFoot">盘中分时与情绪曲线 · 每 30s 刷新</span><span class="foot-right">研究用途，不构成投资建议</span></footer>';
    var market = $("#marketCard");
    main.insertBefore(sec, market ? market.nextSibling : main.firstChild);
  }

  function showTodayMessage(kind, conf) {
    var wrap = kind === "side" ? $("#tdSideCanvas") : $("#tdCanvas");
    var box = kind === "side" ? $("#tdSideEmpty") : $("#tdEmpty");
    if (wrap) wrap.style.visibility = "hidden";
    if (!box) return;
    box.hidden = false;
    renderInto(box, conf);
  }

  function drawIntraday() {
    var canvas = $("#tdCanvas");
    var wrap = $("#tdWrap");
    if (!canvas || !wrap) return;
    var data = state.intraday && state.intraday.data;
    var points = data && data.points ? data.points : [];
    if (!points.length) {
      showTodayMessage("main", {
        title: "分时数据暂未更新",
        message: IS_CLOUD ? "盘中会随行情自动刷新，稍后再看。" : "本地暂无分时数据，盘中运行数据采集后可见。",
        icon: "i-chart"
      });
      return;
    }
    var box = $("#tdEmpty");
    if (box) box.hidden = true;
    canvas.style.visibility = "visible";
    var wrapW = Math.max(240, wrap.clientWidth);
    var cssH = Math.max(200, wrap.clientHeight);
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(wrapW * dpr);
    canvas.height = Math.round(cssH * dpr);
    canvas.style.width = wrapW + "px";
    canvas.style.height = cssH + "px";
    var ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, wrapW, cssH);
    var padT = 12, padL = 8, padR = 58, padB = 20, gap = 8;
    var volH = Math.max(28, Math.round(cssH * 0.16));
    var priceH = cssH - padT - padB - volH - gap;
    var plotW = wrapW - padL - padR;
    var prices = points.map(function (p) { return p.price; });
    var avgs = points.map(function (p) { return p.avg; }).filter(isNum);
    var ref = isNum(data.preClose) ? data.preClose : (state.market ? marketIndexPrice(data.code) : undefined);
    var all = prices.concat(avgs);
    if (isNum(ref)) all.push(ref);
    var lo = Math.min.apply(null, all);
    var hi = Math.max.apply(null, all);
    var span = hi - lo || Math.max(Math.abs(hi) * 0.01, 1);
    lo -= span * 0.08;
    hi += span * 0.08;
    var x = function (i) { return padL + (i / Math.max(1, points.length - 1)) * plotW; };
    var yPrice = function (v) { return padT + priceH * (1 - (v - lo) / (hi - lo)); };
    var maxVol = 1;
    points.forEach(function (p) { maxVol = Math.max(maxVol, p.volume || 0); });
    var yVol = function (v) { return padT + priceH + gap + volH * (1 - v / maxVol); };
    // grid
    ctx.lineWidth = 1;
    ctx.strokeStyle = "rgba(230,237,247,0.06)";
    ctx.font = KLINE_FONT;
    ctx.fillStyle = "#5C6B80";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    niceTicks(lo, hi, 4).forEach(function (v) {
      var y = Math.round(yPrice(v)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(wrapW - padR + 2, y);
      ctx.stroke();
      ctx.fillText(fmtKlineTick(v), wrapW - padR + 7, y);
    });
    // 参考价（昨收）
    if (isNum(ref)) {
      ctx.save();
      ctx.setLineDash([3, 3]);
      ctx.strokeStyle = "rgba(230,237,247,0.28)";
      var ry = Math.round(yPrice(ref)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(padL, ry);
      ctx.lineTo(wrapW - padR + 2, ry);
      ctx.stroke();
      ctx.restore();
    }
    // 成交量
    var barW = Math.max(1, plotW / Math.max(points.length, 1) - 1);
    points.forEach(function (p, i) {
      var prev = i > 0 ? points[i - 1].price : p.price;
      ctx.fillStyle = p.price >= prev ? "rgba(255,77,79,0.5)" : "rgba(34,197,94,0.5)";
      var h = Math.max(1, (p.volume / maxVol) * (volH - 2));
      ctx.fillRect(x(i) - barW / 2, yVol(0) - h, barW, h);
    });
    // 分时线
    var rising = prices[prices.length - 1] >= (isNum(ref) ? ref : prices[0]);
    ctx.strokeStyle = rising ? "#FF4D4F" : "#22C55E";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    points.forEach(function (p, i) {
      if (i === 0) ctx.moveTo(x(i), yPrice(p.price));
      else ctx.lineTo(x(i), yPrice(p.price));
    });
    ctx.stroke();
    // 均价线
    if (avgs.length) {
      ctx.strokeStyle = "#F5A623";
      ctx.lineWidth = 1.1;
      ctx.beginPath();
      var started = false;
      points.forEach(function (p, i) {
        if (!isNum(p.avg)) { started = false; return; }
        if (!started) { ctx.moveTo(x(i), yPrice(p.avg)); started = true; }
        else ctx.lineTo(x(i), yPrice(p.avg));
      });
      ctx.stroke();
    }
    // 时间刻度
    ctx.fillStyle = "#5C6B80";
    ctx.textBaseline = "top";
    var labels = [];
    points.forEach(function (p, i) {
      if (/^(09:30|10:30|11:30|14:00|15:00)$/.test(p.ts)) labels.push(i);
    });
    if (!labels.length) {
      var step = Math.max(1, Math.ceil(points.length / 5));
      for (var i = 0; i < points.length; i += step) labels.push(i);
      if (labels[labels.length - 1] !== points.length - 1) labels.push(points.length - 1);
    }
    labels.forEach(function (i) {
      var label = points[i].ts;
      ctx.textAlign = i === 0 ? "left" : i === points.length - 1 ? "right" : "center";
      ctx.fillText(label, i === 0 ? padL : i === points.length - 1 ? wrapW - padR : x(i), cssH - padB + 5);
    });
    ctx.textAlign = "left";
    var last = points[points.length - 1];
    var chg = isNum(ref) && ref ? last.price / ref - 1 : null;
    setText($("#tdPrice"), fmtKlineTick(last.price));
    if ($("#tdPrice")) {
      $("#tdPrice").className = "num " + (isNum(chg) ? (chg > 0 ? "up" : chg < 0 ? "down" : "") : "");
    }
    setText($("#tdChg"), isNum(chg) ? (chg > 0 ? "+" : "") + (chg * 100).toFixed(2) + "%" : DASH);
    if ($("#tdChg")) $("#tdChg").className = "num " + (isNum(chg) ? (chg > 0 ? "up" : chg < 0 ? "down" : "") : "");
    setText($("#tdDate"), data.tradeDate || "");
    setText($("#tdMeta"), todayIndexName(state.intraday.code) + " · " + (data.tradeDate || "—") + " · " + (isNum(ref) ? "昨收 " + fmtKlineTick(ref) : ""));
    canvas.dataset.ready = "1";
    bindIntradayHover();
    state.intraday.geo = { points: points, x: x, yPrice: yPrice, wrapW: wrapW, padL: padL, padR: padR, last: last, ref: ref };
  }

  function marketIndexPrice(code) {
    var indices = (state.market && state.market.indices) || [];
    for (var i = 0; i < indices.length; i += 1) {
      if (indices[i].code === code) return indices[i].price;
    }
    return undefined;
  }

  function bindIntradayHover() {
    var canvas = $("#tdCanvas");
    if (!canvas || canvas.dataset.hoverBound === "1") return;
    canvas.dataset.hoverBound = "1";
    var pending = 0;
    canvas.addEventListener("pointermove", function (ev) {
      if (pending) return;
      pending = requestAnimationFrame(function () {
        pending = 0;
        var geo = state.intraday.geo;
        if (!geo) return;
        var rect = canvas.getBoundingClientRect();
        var px = ev.clientX - rect.left;
        var ratio = clamp((px - geo.padL) / Math.max(1, geo.wrapW - geo.padL - geo.padR), 0, 1);
        var idx = Math.round(ratio * (geo.points.length - 1));
        var pt = geo.points[idx];
        var tip = $("#tdTip");
        if (!tip || !pt) return;
        var chg = isNum(geo.ref) && geo.ref ? pt.price / geo.ref - 1 : null;
        tip.innerHTML =
          '<div class="kt-row"><span>时间</span><b>' + esc(pt.ts) + "</b></div>" +
          '<div class="kt-row"><span>价格</span><b class="' + (isNum(chg) ? (chg > 0 ? "up" : chg < 0 ? "down" : "flat") : "") + '">' + esc(fmtKlineTick(pt.price)) + "</b></div>" +
          '<div class="kt-row"><span>涨跌</span><b class="' + (isNum(chg) ? (chg > 0 ? "up" : chg < 0 ? "down" : "flat") : "") + '">' + esc(isNum(chg) ? (chg > 0 ? "+" : "") + (chg * 100).toFixed(2) + "%" : DASH) + "</b></div>" +
          (isNum(pt.avg) ? '<div class="kt-row"><span>均价</span><b>' + esc(fmtKlineTick(pt.avg)) + "</b></div>" : "") +
          '<div class="kt-row"><span>成交量</span><b>' + esc(fmtVolumeShou(pt.volume)) + "</b></div>";
        show(tip, true);
        var tw = tip.offsetWidth || 150;
        var tx = px + 14;
        if (tx + tw > geo.wrapW - 4) tx = px - tw - 14;
        tip.style.transform = "translate3d(" + Math.round(clamp(tx, 4, geo.wrapW - tw - 4)) + "px," + Math.round(12) + "px,0)";
      });
    });
    canvas.addEventListener("pointerleave", function () {
      show($("#tdTip"), false);
    });
  }

  function normMarketHistory(payload) {
    var data = payload && payload.data && typeof payload.data === "object" ? payload.data : payload;
    if (!data || typeof data !== "object") return null;
    var raw = Array.isArray(data) ? data : (data.points || data.items || data.series || []);
    if (!Array.isArray(raw) || !raw.length) return null;
    var points = [];
    raw.forEach(function (pt) {
      if (!pt || typeof pt !== "object") return;
      var snap = pt.payload && typeof pt.payload === "object" ? pt.payload : pt;
      var sent = snap.sentiment && typeof snap.sentiment === "object" ? snap.sentiment : snap;
      var act = snap.activity && typeof snap.activity === "object" ? snap.activity : snap;
      var ts = fmtIntradayTime(pt.asof || pt.ts || pt.time || pt.t);
      var score = num(pick(sent, "score", "sentiment_score", "temperature"));
      var up = num(pick(act, "up", "advance", "up_count"));
      var down = num(pick(act, "down", "decline", "down_count"));
      if (!ts) return;
      points.push({ ts: ts, score: score, up: up || 0, down: down || 0, flat: num(act.flat) || 0 });
    });
    if (!points.length) return null;
    points.sort(function (a, b) { return a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : 0; });
    return { date: data.trade_date || data.date || "", points: points };
  }

  function drawMarketHistory() {
    var canvas = $("#tdSideCanvas");
    var wrap = canvas && canvas.parentNode;
    if (!canvas || !wrap) return;
    var data = state.marketHistory;
    var points = data && data.points ? data.points : [];
    var empty = $("#tdSideEmpty");
    if (!points.length) {
      canvas.style.visibility = "hidden";
      if (empty) {
        empty.hidden = false;
        renderInto(empty, {
          title: "情绪曲线暂未更新",
          message: "盘中会随涨跌家数一起刷新。",
          icon: "i-chart"
        });
      }
      setText($("#tdSideMeta"), "—");
      return;
    }
    if (empty) empty.hidden = true;
    canvas.style.visibility = "visible";
    var wrapW = Math.max(200, wrap.clientWidth);
    var cssH = Math.max(170, wrap.clientHeight);
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(wrapW * dpr);
    canvas.height = Math.round(cssH * dpr);
    canvas.style.width = wrapW + "px";
    canvas.style.height = cssH + "px";
    var ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, wrapW, cssH);
    var padT = 10, padL = 6, padR = 8, padB = 18, volH = 36, gap = 6;
    var plotH = cssH - padT - padB - volH - gap;
    var plotW = wrapW - padL - padR;
    var hasScore = points.some(function (p) { return isNum(p.score); });
    var maxBreadth = 1;
    points.forEach(function (p) { maxBreadth = Math.max(maxBreadth, p.up + p.down); });
    var x = function (i) { return padL + (i / Math.max(1, points.length - 1)) * plotW; };
    var yScore = function (v) { return padT + plotH * (1 - v / 100); };
    var yBreadthBase = padT + plotH + gap + volH;
    // 情绪网格
    ctx.strokeStyle = "rgba(230,237,247,0.06)";
    ctx.lineWidth = 1;
    [0.5, 1].forEach(function (r) {
      var y = Math.round(padT + plotH * (1 - r)) + 0.5;
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(wrapW - padR, y); ctx.stroke();
    });
    if (hasScore) {
      ctx.strokeStyle = "#4C8DFF";
      ctx.lineWidth = 1.3;
      ctx.beginPath();
      var started = false;
      points.forEach(function (p, i) {
        if (!isNum(p.score)) { started = false; return; }
        if (!started) { ctx.moveTo(x(i), yScore(p.score)); started = true; }
        else ctx.lineTo(x(i), yScore(p.score));
      });
      ctx.stroke();
      var lastScore = points[points.length - 1].score;
      if (isNum(lastScore)) {
        ctx.fillStyle = "#4C8DFF";
        ctx.font = KLINE_FONT;
        ctx.textAlign = "right";
        ctx.textBaseline = "middle";
        ctx.fillText(fmtKlineTick(lastScore), wrapW - padR, yScore(lastScore));
      }
    }
    // 涨跌家数堆叠柱
    var barW = Math.max(1, plotW / Math.max(points.length, 1) - 1);
    points.forEach(function (p, i) {
      var total = p.up + p.down;
      if (!total) return;
      var upH = (p.up / Math.max(maxBreadth, 1)) * (volH - 2);
      var downH = (p.down / Math.max(maxBreadth, 1)) * (volH - 2);
      ctx.fillStyle = "rgba(255,77,79,0.55)";
      ctx.fillRect(x(i) - barW / 2, yBreadthBase - upH, barW, upH);
      ctx.fillStyle = "rgba(34,197,94,0.55)";
      ctx.fillRect(x(i) - barW / 2, yBreadthBase - upH - downH, barW, downH);
    });
    // 时间标签（首尾）
    ctx.fillStyle = "#5C6B80";
    ctx.font = KLINE_FONT;
    ctx.textBaseline = "top";
    ctx.textAlign = "left";
    ctx.fillText(points[0].ts, padL, cssH - padB + 5);
    ctx.textAlign = "right";
    ctx.fillText(points[points.length - 1].ts, wrapW - padR, cssH - padB + 5);
    ctx.textAlign = "left";
    var last = points[points.length - 1];
    setText($("#tdSideMeta"), (data.date ? data.date + " · " : "") + "情绪 " + (isNum(last.score) ? fmtKlineTick(last.score) : DASH) + " · 涨 " + fmtInt(last.up) + " / 跌 " + fmtInt(last.down));
  }

  function loadIntraday(code, options) {
    var opts = options || {};
    var target = code || state.intraday.code || "sh000001";
    state.intraday.code = target;
    renderTodayTabs();
    var cached = state.intraday.cache.get(target);
    if (cached && !opts.force) {
      state.intraday.data = cached;
      drawIntraday();
      return Promise.resolve({ kind: "ok" });
    }
    var reqId = ++state.intraday.req;
    return getJSON(EP.intraday + "?code=" + encodeURIComponent(target)).then(function (r) {
      if (reqId !== state.intraday.req) return { kind: "stale" };
      if (r.challenge) { noteChallenge(); return { kind: "challenge" }; }
      if (r.rateLimited) { startRateLimit(r.retryAfter); return { kind: "ratelimit" }; }
      if (r.error) {
        state.intraday.data = null;
        drawIntraday();
        return { kind: r.error.status === 404 ? "empty" : "error" };
      }
      var data = normIntradayPayload(r.data);
      if (!data) {
        state.intraday.data = null;
        drawIntraday();
        return { kind: "empty" };
      }
      data.code = data.code || target;
      data.name = data.name || todayIndexName(target);
      state.intraday.cache.set(target, data);
      state.intraday.data = data;
      drawIntraday();
      return { kind: "ok" };
    });
  }

  function renderTodayTabs() {
    var host = $("#tdTabs");
    if (!host) return;
    $$(".td-tab", host).forEach(function (btn) {
      btn.setAttribute("aria-selected", btn.dataset.code === state.intraday.code ? "true" : "false");
    });
  }

  function loadMarketHistory() {
    var date = (state.market && state.market.trade_date) || "";
    var url = EP.marketHistory + (date ? "?date=" + encodeURIComponent(date) : "");
    return getJSON(url).then(function (r) {
      if (r.challenge) { noteChallenge(); return { kind: "challenge" }; }
      if (r.rateLimited) { startRateLimit(r.retryAfter); return { kind: "ratelimit" }; }
      if (r.error) {
        state.marketHistory = null;
        drawMarketHistory();
        return { kind: r.error.status === 404 ? "empty" : "error" };
      }
      state.marketHistory = normMarketHistory(r.data);
      drawMarketHistory();
      return { kind: state.marketHistory ? "ok" : "empty" };
    });
  }

  /* ============================== 午评 / 收评 ============================== */
  function createSummaryPanel() {
    var main = $("#main");
    if (!main || $("#summaryCard")) return;
    var sec = document.createElement("section");
    sec.id = "summaryCard";
    sec.className = "card span-4 summary-card";
    sec.setAttribute("aria-labelledby", "sumTitle");
    sec.innerHTML =
      '<header class="card-head">' +
        '<h2 class="card-title" id="sumTitle">午评 · 收评<span class="card-sub">研究用途</span></h2>' +
        '<div class="card-meta"><span id="sumDate">—</span></div>' +
      '</header>' +
      '<div class="summary-body" id="sumBody">' +
        '<article class="sum-block" id="sumMid">' +
          '<div class="sum-head"><span class="sum-tag">午盘总结</span><span class="sum-time num" id="sumMidTime">—</span></div>' +
          '<div class="sum-text" id="sumMidText"><span class="sum-placeholder" id="sumMidPlaceholder">午评将于 11:35 生成</span></div>' +
        '</article>' +
        '<article class="sum-block" id="sumClose">' +
          '<div class="sum-head"><span class="sum-tag">收盘总结</span><span class="sum-time num" id="sumCloseTime">—</span></div>' +
          '<div class="sum-text" id="sumCloseText"><span class="sum-placeholder" id="sumClosePlaceholder">收盘总结将于 15:30 生成</span></div>' +
        '</article>' +
      '</div>' +
      '<footer class="card-foot"><span>研究用途，不构成投资建议</span><span class="foot-right" id="sumFoot">—</span></footer>';
    var today = $("#todayCard");
    var regime = $("#regimeCard");
    var anchor = today ? today.nextSibling : (regime ? regime.nextSibling : main.firstChild);
    main.insertBefore(sec, anchor);
  }

  function summaryTextOf(item) {
    if (!item) return "";
    if (typeof item === "string") return item;
    return String(item.summary_text || item.content || item.body || item.text || item.summary || item.markdown || "").trim();
  }

  function summaryTimeOf(item) {
    if (!item || typeof item === "string") return "";
    return String(item.generated_at || item.created_at || item.asof || item.ts || item.time || "");
  }

  function renderSummaryText(kind, item, placeholderEl) {
    var textEl = kind === "mid" ? $("#sumMidText") : $("#sumCloseText");
    var timeEl = kind === "mid" ? $("#sumMidTime") : $("#sumCloseTime");
    if (!textEl) return;
    var text = summaryTextOf(item);
    if (!text) {
      var p = shParts(new Date());
      var mins = p ? Number(p.hh) * 60 + Number(p.mm) : 0;
      var label = kind === "mid" ? "午评将于 11:35 生成" : "收盘总结将于 15:30 生成";
      if (kind === "mid" && mins > 11 * 60 + 35) label = "今日午评暂未生成";
      if (kind === "close" && mins > 15 * 60 + 30) label = "今日收盘总结暂未生成";
      if (placeholderEl) setText(placeholderEl, label);
      if (textEl) textEl.innerHTML = '<span class="sum-placeholder">' + esc(label) + "</span>";
      if (timeEl) setText(timeEl, DASH);
      return;
    }
    textEl.innerHTML = text.split(/\n+/).map(function (p) { return "<p>" + esc(p) + "</p>"; }).join("");
    if (timeEl) setText(timeEl, summaryTimeOf(item) ? fmtClock(summaryTimeOf(item)) : DASH);
  }

  function loadSummary() {
    return getJSON(EP.summary).then(function (r) {
      if (r.challenge) { noteChallenge(); return { kind: "challenge" }; }
      if (r.rateLimited) { startRateLimit(r.retryAfter); return { kind: "ratelimit" }; }
      if (r.error) {
        state.summary = null;
        renderSummary();
        return { kind: r.error.status === 404 ? "empty" : "error" };
      }
      var data = r.data || {};
      var midday = data.midday || data.noon || data.mid || null;
      var close = data.close || data.closing || data.final || null;
      if (!midday && data.session === "midday") midday = data;
      if (!close && data.session === "close") close = data;
      if (!midday && !close && Array.isArray(data.items)) {
        data.items.forEach(function (item) {
          var kindName = String(item.kind || item.type || item.session || "").toLowerCase();
          if (kindName.indexOf("mid") >= 0 || kindName.indexOf("noon") >= 0) midday = item;
          else if (kindName.indexOf("close") >= 0 || kindName.indexOf("final") >= 0) close = item;
        });
      }
      if (!midday && !close && data.title) close = data;
      state.summary = { midday: midday, close: close, date: data.trade_date || data.date || "", updatedAt: data.generated_at || r.data.updated_at || "" };
      renderSummary();
      return { kind: midday || close ? "ok" : "empty" };
    });
  }

  function renderSummary() {
    if (!$("#sumBody")) return;
    var summary = state.summary || {};
    renderSummaryText("mid", summary.midday, $("#sumMidPlaceholder"));
    renderSummaryText("close", summary.close, $("#sumClosePlaceholder"));
    var date = summary.date || (state.market && state.market.trade_date) || "";
    setText($("#sumDate"), date || DASH);
    var time = summary.updatedAt ? fmtClock(summary.updatedAt) : "";
    setText($("#sumFoot"), time ? "更新 " + time : "今日总结生成后自动刷新");
  }

  /* ============================== 市场轮询（30s 实时条） ============================== */
  function scheduleMarketPoll() {
    if (state.marketPollTimer) clearTimeout(state.marketPollTimer);
    if (state.hidden) return;
    var delay = isTradingSession() ? 30000 : 5 * 60_000;
    state.marketPollTimer = setTimeout(function () {
      state.marketPollTimer = null;
      if (state.hidden) return;
      if (state.rateLimitUntil > Date.now() || state.gate.blocked) { scheduleMarketPoll(); return; }
      void pollMarket();
    }, delay);
  }

  function pollMarket() {
    var jobs = [loadMarket("live")];
    if (state.todayVisible !== false) {
      jobs.push(loadIntraday(state.intraday.code, { force: true }));
    }
    return Promise.all(jobs).then(function () {
      scheduleMarketPoll();
    }, function () {
      scheduleMarketPoll();
    });
  }

  function startRealtime() {
    scheduleMarketPoll();
    if (state.clockTimer) clearInterval(state.clockTimer);
    state.clockTimer = setInterval(updateRealtimeClock, 1000);
    updateRealtimeClock();
  }


  /* ============================== 市场全景（板块榜 / 资金流） ============================== */
  function ensurePanoramaSections() {
    var card = $("#marketCard");
    if (!card || $("#panoGrid")) return;
    var grid = document.createElement("div");
    grid.className = "pano-grid";
    grid.id = "panoGrid";
    grid.innerHTML =
      '<div class="pano-block"><div class="col-head"><span>板块涨幅榜 Top10</span><span class="col-meta">当日 / 5日 / 净流入(亿)</span></div>' +
        '<div class="pano-list" id="panoLeaders"><div class="mkt-empty">暂无板块数据</div></div></div>' +
      '<div class="pano-block"><div class="col-head"><span>资金净流入 Top10</span><span class="col-meta">行业 + 概念</span></div>' +
        '<div class="pano-list" id="panoFlow"><div class="mkt-empty">暂无资金流数据</div></div></div>';
    card.appendChild(grid);
  }

  function panoRowHtml(item, index) {
    var kind = item.kind === "concept" ? "概念" : item.kind === "industry" ? "行业" : "";
    var d1 = num(item.pct_1d);
    var d5 = num(item.pct_5d);
    var flow = num(item.net_inflow);
    return '<div class="pano-row">' +
      '<span class="pano-rank num">' + (index + 1) + "</span>" +
      '<span class="pano-name">' + esc(item.name || "—") + (kind ? '<i class="rg-kind">' + kind + "</i>" : "") + "</span>" +
      '<span class="pano-cell num ' + (isNum(d1) && d1 > 0 ? "up" : isNum(d1) && d1 < 0 ? "down" : "") + '">' +
        esc(isNum(d1) ? (d1 > 0 ? "+" : "") + fmtFixed(d1, 2) + "%" : DASH) + "</span>" +
      '<span class="pano-cell num ' + (isNum(d5) && d5 > 0 ? "up" : isNum(d5) && d5 < 0 ? "down" : "") + '">' +
        esc(isNum(d5) ? (d5 > 0 ? "+" : "") + fmtFixed(d5, 2) + "%" : DASH) + "</span>" +
      '<span class="pano-cell num ' + (isNum(flow) && flow > 0 ? "up" : isNum(flow) && flow < 0 ? "down" : "") + '">' +
        esc(isNum(flow) ? (flow > 0 ? "+" : "") + fmtFixed(flow, 1) : DASH) + "</span>" +
      "</div>";
  }

  function renderPanorama() {
    ensurePanoramaSections();
    var leaders = (state.regime && state.regime.leaders) || [];
    var top = leaders.slice(0, 10);
    var box = $("#panoLeaders");
    if (box) {
      box.innerHTML = top.length
        ? top.map(function (item, i) { return panoRowHtml(item, i); }).join("")
        : '<div class="mkt-empty">暂无板块数据（等待大盘状态快照）</div>';
    }
    var flowBox = $("#panoFlow");
    if (flowBox) {
      var flow = leaders.slice().sort(function (a, b) { return (num(b.net_inflow) || -Infinity) - (num(a.net_inflow) || -Infinity); }).slice(0, 10);
      flowBox.innerHTML = flow.length
        ? flow.map(function (item, i) {
            var value = num(item.net_inflow);
            var kind = item.kind === "concept" ? "概念" : item.kind === "industry" ? "行业" : "";
            return '<div class="pano-row pano-flow-row">' +
              '<span class="pano-rank num">' + (i + 1) + "</span>" +
              '<span class="pano-name">' + esc(item.name || "—") + (kind ? '<i class="rg-kind">' + kind + "</i>" : "") + "</span>" +
              '<span class="pano-bar"><i class="' + (isNum(value) && value >= 0 ? "up" : "down") + '" style="width:' + clamp(Math.abs(value || 0) / Math.max(1, Math.abs(num(leaders[0] && leaders[0].net_inflow) || 100)) * 100, 4, 100).toFixed(1) + '%"></i></span>' +
              '<span class="pano-cell num ' + (isNum(value) && value > 0 ? "up" : isNum(value) && value < 0 ? "down" : "") + '">' +
                esc(isNum(value) ? (value > 0 ? "+" : "") + fmtFixed(value, 1) : DASH) + "</span>" +
              "</div>";
          }).join("")
        : '<div class="mkt-empty">暂无资金流数据</div>';
    }
  }

  /* ============================== 全市场搜索 ============================== */
  function highlightMatch(text, query) {
    var value = String(text || "");
    var q = String(query || "").trim();
    if (!q) return esc(value);
    var idx = value.toLowerCase().indexOf(q.toLowerCase());
    if (idx < 0) return esc(value);
    return esc(value.slice(0, idx)) + "<mark>" + esc(value.slice(idx, idx + q.length)) + "</mark>" + esc(value.slice(idx + q.length));
  }

  function initGlobalSearch() {
    var input = $("#stockQuery");
    var btn = $("#stockQueryBtn");
    var panel = $("#searchPanel");
    if (!input || !btn || !panel) return;
    var search = { items: [], active: -1, timer: 0, req: 0, open: false };
    state.search = search;

    function close() {
      search.open = false;
      search.active = -1;
      panel.hidden = true;
      if (input) input.setAttribute("aria-expanded", "false");
    }
    function open() {
      search.open = true;
      panel.hidden = false;
      if (input) input.setAttribute("aria-expanded", "true");
    }
    function render() {
      if (!search.items.length) {
        panel.innerHTML = '<div class="search-empty">暂无匹配</div>';
        return;
      }
      panel.innerHTML = search.items.map(function (item, i) {
        return '<button type="button" class="search-item' + (i === search.active ? " is-active" : "") + '" data-symbol="' + esc(item.symbol) + '" role="option" aria-selected="' + (i === search.active ? "true" : "false") + '">' +
          '<span class="search-sym num">' + highlightMatch(item.symbol, input.value) + "</span>" +
          '<span class="search-name">' + highlightMatch(item.name, input.value) + "</span>" +
          (item.initials ? '<span class="search-ini num">' + highlightMatch(item.initials, input.value) + "</span>" : "") +
          "</button>";
      }).join("");
    }
    function applyItems(items) {
      search.items = Array.isArray(items) ? items : [];
      search.active = search.items.length ? 0 : -1;
      render();
      if (search.open) open();
    }
    function selectSymbol(symbol, name) {
      if (!/^\d{6}$/.test(symbol)) return;
      input.value = symbol;
      close();
      input.blur();
      state.kline.auto = false;
      loadKline(symbol, { force: false });
      queryStockCard(symbol, name);
      announce("已切换到 " + symbol + (name ? " " + name : ""));
    }
    function queryStockCard(symbol, name) {
      getJSON(EP.stock + "?symbol=" + encodeURIComponent(symbol)).then(function (r) {
        if (r.challenge) { noteChallenge(); return; }
        if (r.rateLimited) { startRateLimit(r.retryAfter); return; }
        if (r.error || !r.data) return;
        if (r.data.ok === false) return;
        renderStockResult(r.data);
      });
    }
    function runQuery() {
      var q = String(input.value || "").trim();
      if (!q) { close(); return; }
      if (search.timer) { clearTimeout(search.timer); search.timer = 0; }
      var reqId = ++search.req;
      getJSON(EP.search + "?q=" + encodeURIComponent(q) + "&limit=12").then(function (r) {
        if (reqId !== search.req) return;
        if (r.challenge) { noteChallenge(); return; }
        if (r.rateLimited) { startRateLimit(r.retryAfter); return; }
        if (r.error || !r.data) { applyItems([]); return; }
        applyItems(r.data.items || []);
      });
    }
    function scheduleQuery() {
      if (search.timer) clearTimeout(search.timer);
      var q = String(input.value || "").trim();
      if (!q) { search.items = []; close(); return; }
      search.timer = setTimeout(function () { search.timer = 0; runQuery(); }, 200);
    }
    input.addEventListener("focus", function () {
      if (String(input.value || "").trim()) { scheduleQuery(); }
    });
    input.addEventListener("input", scheduleQuery);
    input.addEventListener("keydown", function (ev) {
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        ev.preventDefault();
        if (!search.open) { runQuery(); return; }
        if (!search.items.length) return;
        search.active = (search.active + (ev.key === "ArrowDown" ? 1 : -1) + search.items.length) % search.items.length;
        render();
      } else if (ev.key === "Enter") {
        ev.preventDefault();
        if (search.open && search.active >= 0 && search.items[search.active]) {
          var item = search.items[search.active];
          selectSymbol(item.symbol, item.name);
          return;
        }
        var q = String(input.value || "").trim();
        if (/^\d{6}$/.test(q)) { selectSymbol(q, ""); return; }
        if (search.items.length) { selectSymbol(search.items[0].symbol, search.items[0].name); return; }
        if (q) {
          search.items = [];
          render();
          open();
        }
      } else if (ev.key === "Escape") {
        close();
        input.blur();
      }
    });
    input.addEventListener("blur", function () {
      setTimeout(function () {
        if (document.activeElement && panel.contains(document.activeElement)) return;
        close();
      }, 140);
    });
    panel.addEventListener("mousedown", function (ev) {
      ev.preventDefault();
    });
    panel.addEventListener("click", function (ev) {
      var btnEl = ev.target.closest ? ev.target.closest(".search-item") : null;
      if (!btnEl) return;
      var item = search.items.find(function (it) { return it.symbol === btnEl.dataset.symbol; });
      selectSymbol(btnEl.dataset.symbol, item ? item.name : "");
    });
    btn.addEventListener("click", function () {
      if (search.open && search.active >= 0 && search.items[search.active]) {
        var item = search.items[search.active];
        selectSymbol(item.symbol, item.name);
      } else if (/^\d{6}$/.test(String(input.value || "").trim())) {
        selectSymbol(String(input.value).trim(), "");
      } else {
        runQuery();
      }
    });
  }

  /* ============================== 回测面板补完整（净值/月度/成本） ============================== */
  function ensureBacktestExtras() {
    var metrics = $("#btMetrics");
    if (!metrics || $("#btExtras")) return;
    var box = document.createElement("div");
    box.id = "btExtras";
    box.className = "bt-extras";
    box.hidden = true;
    box.innerHTML =
      '<div class="bt-extra-row" id="btExtraMetrics"></div>' +
      '<div class="bt-chart-grid">' +
        '<div class="bt-chart-block"><div class="col-head"><span>策略净值</span><span class="col-meta">归一化 · 虚线 = 初始资金</span></div>' +
          '<div class="bt-eq-wrap"><canvas id="btEquityCanvas" role="img" aria-label="策略净值曲线"></canvas></div></div>' +
        '<div class="bt-chart-block"><div class="col-head"><span>月度收益</span><span class="col-meta">最近 18 个月</span></div>' +
          '<div class="bt-monthly" id="btMonthly"></div></div>' +
      '</div>' +
      '<p class="bt-costs" id="btCosts"></p>';
    metrics.parentNode.insertBefore(box, metrics.nextSibling);
  }

  function drawBtEquity(points) {
    var canvas = $("#btEquityCanvas");
    if (!canvas || !points.length) return;
    var wrap = canvas.parentNode;
    var cssW = Math.max(240, wrap.clientWidth);
    var cssH = Math.max(120, wrap.clientHeight || 130);
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    canvas.style.width = cssW + "px";
    canvas.style.height = cssH + "px";
    var ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);
    var base = points[0].equity || 1;
    var values = points.map(function (p) { return p.equity / base; });
    var lo = Math.min.apply(null, values.concat([1]));
    var hi = Math.max.apply(null, values.concat([1]));
    var span = hi - lo || 0.01;
    lo -= span * 0.12;
    hi += span * 0.12;
    var padT = 8, padB = 14, padL = 4, padR = 6;
    var x = function (i) { return padL + (i / Math.max(1, values.length - 1)) * (cssW - padL - padR); };
    var y = function (v) { return padT + (cssH - padT - padB) * (1 - (v - lo) / (hi - lo)); };
    ctx.strokeStyle = "rgba(230,237,247,0.06)";
    ctx.lineWidth = 1;
    [0.33, 0.66].forEach(function (r) {
      var gy = Math.round(padT + (cssH - padT - padB) * r) + 0.5;
      ctx.beginPath(); ctx.moveTo(padL, gy); ctx.lineTo(cssW - padR, gy); ctx.stroke();
    });
    ctx.save();
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = "rgba(230,237,247,0.32)";
    var by = Math.round(y(1)) + 0.5;
    ctx.beginPath(); ctx.moveTo(padL, by); ctx.lineTo(cssW - padR, by); ctx.stroke();
    ctx.restore();
    var rising = values[values.length - 1] >= 1;
    ctx.strokeStyle = rising ? "#FF4D4F" : "#22C55E";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    values.forEach(function (v, i) {
      if (i === 0) ctx.moveTo(x(i), y(v));
      else ctx.lineTo(x(i), y(v));
    });
    ctx.stroke();
    ctx.fillStyle = "#5C6B80";
    ctx.font = KLINE_FONT;
    ctx.textBaseline = "middle";
    ctx.textAlign = "left";
    ctx.fillText("1.00", padL + 2, Math.round(y(1)));
    ctx.fillText(values[0].toFixed(2), padL + 2, padT + 2);
    ctx.fillText(values[values.length - 1].toFixed(2), cssW - padR - 26, cssH - padB - 2);
  }

  function monthlyReturns(points) {
    if (!points || points.length < 2) return [];
    var months = [];
    var lastByMonth = {};
    points.forEach(function (p) {
      var key = String(p.ts || "").slice(0, 7);
      if (!key) return;
      lastByMonth[key] = p.equity;
    });
    var keys = Object.keys(lastByMonth).sort();
    var prev = null;
    keys.forEach(function (key) {
      var value = lastByMonth[key];
      months.push({ month: key, ret: prev ? value / prev - 1 : null });
      prev = value;
    });
    return months.filter(function (m) { return isNum(m.ret); }).slice(-18);
  }

  function renderBacktestExtras(bt) {
    ensureBacktestExtras();
    var box = $("#btExtras");
    if (!box) return;
    var curve = bt && bt.equityCurve ? bt.equityCurve : [];
    if (!bt || curve.length < 2) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    var first = curve[0].equity;
    var last = curve[curve.length - 1].equity;
    var t0 = Date.parse(curve[0].ts);
    var t1 = Date.parse(curve[curve.length - 1].ts);
    var days = Number.isFinite(t0) && Number.isFinite(t1) ? Math.max(1, (t1 - t0) / 86400000) : 0;
    var years = days > 0 ? days / 365.25 : 0;
    var totalReturn = first ? last / first - 1 : undefined;
    var annualized = years >= 0.2 && totalReturn !== undefined && 1 + totalReturn > 0
      ? Math.pow(1 + totalReturn, 1 / years) - 1 : undefined;
    var trades = bt.trades || {};
    var extra = [
      { label: "年化收益", value: isNum(annualized) ? fmtSignedPct(annualized) : "样本期过短", tone: annualized },
      { label: "交易笔数", value: isNum(trades.total) ? fmtInt(trades.total) : DASH },
      { label: "平均每笔", value: isNum(bt.avgReturn) ? fmtSignedPct(bt.avgReturn) : DASH, tone: bt.avgReturn },
      { label: "盈亏比", value: isNum(bt.payoff) ? fmtFixed(bt.payoff, 2) : DASH },
      { label: "样本量 n", value: isNum(bt.n) ? fmtInt(bt.n) : DASH },
      { label: "净值区间", value: curve.length + " 天" }
    ];
    var metricsEl = $("#btExtraMetrics");
    if (metricsEl) {
      metricsEl.innerHTML = extra.map(function (item) {
        var cls = isNum(item.tone) ? (item.tone > 0 ? "up" : item.tone < 0 ? "down" : "") : "";
        return '<div class="bt-extra-item"><span>' + esc(item.label) + '</span><b class="num ' + cls + '">' + esc(item.value) + "</b></div>";
      }).join("");
    }
    drawBtEquity(curve);
    var monthly = monthlyReturns(curve);
    var monthlyEl = $("#btMonthly");
    if (monthlyEl) {
      if (!monthly.length) {
        monthlyEl.innerHTML = '<div class="mkt-empty">月度样本不足</div>';
      } else {
        var maxAbs = monthly.reduce(function (m, item) { return Math.max(m, Math.abs(item.ret)); }, 0.001);
        monthlyEl.innerHTML = '<div class="bt-months">' + monthly.map(function (item) {
          var pct = item.ret * 100;
          var h = clamp(Math.abs(item.ret) / maxAbs * 100, 3, 100);
          var cls = item.ret >= 0 ? "up" : "down";
          return '<div class="bt-month" title="' + esc(item.month) + " " + esc(fmtSignedPct(item.ret)) + '">' +
            '<div class="bt-month-bar"><i class="' + cls + '" style="height:' + h.toFixed(1) + '%"></i></div>' +
            '<span class="num">' + esc(item.month.slice(2)) + "</span></div>";
        }).join("") + "</div>";
      }
    }
    var costs = $("#btCosts");
    if (costs) {
      costs.textContent = "成本假设（契约 §6）：佣金 0.025%（最低 5 元，双向）· 印花税 0.05%（卖出）· 过户费 0.001%（双向）· 滑点 5bps";
    }
  }


  /* ============================== 重大事件（利好利空） ============================== */
  function createEventsPanel() {
    var main = $("#main");
    if (!main || $("#eventsCard")) return;
    var sec = document.createElement("section");
    sec.className = "card span-12";
    sec.id = "eventsCard";
    sec.setAttribute("aria-labelledby", "evTitle");
    sec.innerHTML = '<header class="card-head"><div><p class="eyebrow">事件驱动</p>' +
      '<h2 id="evTitle">重大事件 · 利好利空</h2></div>' +
      '<div class="card-meta"><span id="evStats">—</span></div></header>' +
      '<div id="evList" class="event-list"><div class="mkt-empty">暂无事件数据</div></div>';
    var market = $("#marketCard");
    if (market && market.nextSibling) main.insertBefore(sec, market.nextSibling);
    else main.insertBefore(sec, main.firstChild);
  }
  function loadEvents() {
    return getJSON(EP.events + "?limit=40").then(function (r) {
      if (r.challenge) { noteChallenge(); return { kind: "challenge" }; }
      if (r.rateLimited) { startRateLimit(r.retryAfter); return { kind: "ratelimit" }; }
      if (r.error) { return { kind: r.error.status === 404 ? "empty" : "error" }; }
      var items = (r.data && (r.data.items || (r.data.snapshot && r.data.snapshot.items))) || [];
      state.events = items;
      renderEvents();
      return { kind: items.length ? "ok" : "empty" };
    });
  }
  function renderEvents() {
    var list = $("#evList");
    if (!list) return;
    var items = state.events || [];
    var stats = $("#evStats");
    if (!items.length) {
      list.innerHTML = '<div class="mkt-empty">暂无事件数据</div>';
      if (stats) setText(stats, "—");
      return;
    }
    var bull = items.filter(function (x) { return x.sentiment === "bullish"; }).length;
    var bear = items.filter(function (x) { return x.sentiment === "bearish"; }).length;
    var neutral = items.length - bull - bear;
    if (stats) setText(stats, "利好 " + bull + " · 利空 " + bear + " · 中性 " + neutral);
    list.innerHTML = items.slice(0, 40).map(function (it) {
      var cls = it.sentiment === "bullish" ? "bull" : (it.sentiment === "bearish" ? "bear" : "neu");
      var time = String(it.published_at || "").slice(5, 16).replace("T", " ");
      var syms = (it.symbols || []).join(" ");
      return '<div class="event-item ' + cls + '">' +
        '<span class="event-tag">' + esc(it.label || "中性") + '</span>' +
        '<span class="event-time muted num">' + esc(time) + '</span>' +
        '<span class="event-title">' + esc(it.title) + '</span>' +
        (syms ? '<span class="event-syms muted num">' + esc(syms) + '</span>' : '') +
        '<span class="event-src muted">' + esc(it.source || "") + '</span>' +
      '</div>';
    }).join("");
  }

  /* ============================== K 线主视觉（Canvas 手绘 + 缩放/平移） ============================== */
  var KLINE_MIN_BARS = 20;
  var KLINE_MAX_BARS = 250;
  var KLINE_DEFAULT_BARS = 120;
  var KLINE_PRESETS = [60, 120, 250];
  var KLINE_LOAD_DAYS = 250;          // 一次拉满 250 根，缩放/平移都在本地完成
  var KLINE_AXIS_W = 52;
  var KLINE_FONT = '9px ui-monospace, "SF Mono", Menlo, monospace';
  var KLINE_STORE_KEY = "quant.kline.symbol";
  var KLINE_MA = [
    { key: "ma5", color: "#E6EDF7", label: "MA5" },
    { key: "ma10", color: "#F5A623", label: "MA10" },
    { key: "ma20", color: "#4C8DFF", label: "MA20" }
  ];

  function rememberedKlineSymbol() {
    try {
      var value = window.localStorage ? window.localStorage.getItem(KLINE_STORE_KEY) : "";
      return /^\d{6}$/.test(String(value || "").trim()) ? String(value).trim() : "";
    } catch (e) { return ""; }
  }

  function rememberKlineSymbol(symbol) {
    try {
      if (window.localStorage) window.localStorage.setItem(KLINE_STORE_KEY, symbol);
    } catch (e) { /* 隐私模式忽略 */ }
  }

  function signalNameFor(symbol) {
    for (var i = 0; i < state.signals.length; i += 1) {
      if (state.signals[i].symbol === symbol && state.signals[i].name) return state.signals[i].name;
    }
    for (var j = 0; j < INDEX_TABS.length; j += 1) {
      if (INDEX_TABS[j].code === symbol) return INDEX_TABS[j].name;
    }
    return SYMBOL_NAMES[symbol] || "";
  }

  function createKlinePanel() {
    var main = $("#main");
    if (!main || $("#klineCard")) return;
    var indexTabs = INDEX_TABS.map(function (it) {
      return '<button type="button" class="kline-index-tab" data-symbol="' + it.code + '" title="' + it.name + '">' + it.name.replace("指数", "").replace("成指", "成指") + "</button>";
    }).join("");
    var presets = KLINE_PRESETS.map(function (d) {
      return '<button type="button" class="kline-tab" role="tab" data-count="' + d +
        '" aria-selected="' + (d === KLINE_DEFAULT_BARS ? "true" : "false") + '">' + d + "</button>";
    }).join("");
    var maChips = KLINE_MA.map(function (ma) {
      return '<span><i class="lg-ma ' + ma.key + '"></i>' + ma.label + "</span>";
    }).join("");
    var sec = document.createElement("section");
    sec.id = "klineCard";
    sec.className = "card kline-card";
    sec.setAttribute("aria-labelledby", "klTitle");
    sec.innerHTML =
      '<header class="card-head kline-head">' +
        '<h2 class="card-title kline-title" id="klTitle">' +
          '<span class="kl-symbol" id="klSymbolTitle">—</span>' +
          '<span class="kl-name" id="klNameTitle"></span>' +
          '<span class="card-sub" id="klSub">日K</span>' +
        '</h2>' +
        '<div class="kline-tools">' +
          '<input id="klineSymbol" class="stock-input kline-input" type="text" inputmode="text" maxlength="9" placeholder="代码/指数" aria-label="K线股票代码" />' +
          '<button class="btn btn-icon" id="klineGo" type="button" title="加载 K 线" aria-label="加载 K 线"><svg class="icon" aria-hidden="true"><use href="#i-chart"/></svg></button>' +
        '</div>' +
      '</header>' +
      '<div class="kline-quote" id="klineQuote">' +
        '<div class="kq-main">' +
          '<div class="kq-price-row">' +
            '<b class="kq-price num" id="kqPrice">—</b>' +
            '<span class="kq-chg num" id="kqChg">—</span>' +
            '<span class="kq-date num" id="kqDate">—</span>' +
            '<span class="kq-hover-tag" id="kqHoverTag" hidden>十字光标</span>' +
          '</div>' +
          '<div class="kq-sub num">' +
            '<span>开 <b id="kqOpen">—</b></span><span>高 <b id="kqHigh">—</b></span>' +
            '<span>低 <b id="kqLow">—</b></span><span>量 <b id="kqVolume">—</b></span>' +
            '<span>额 <b id="kqAmount">—</b></span>' +
          '</div>' +
        '</div>' +
        '<div class="kq-ma num" id="kqMa"></div>' +
      '</div>' +
      '<div class="kline-bar">' +
        '<div class="kline-index-tabs" id="klineIndexTabs" role="tablist" aria-label="指数K线">' + indexTabs + '</div>' +
        '<div class="kline-tabs" id="klineRange" role="tablist" aria-label="显示根数">' + presets + '</div>' +
        '<div class="kline-zoom">' +
          '<button class="kline-zoom-btn" id="klineZoomOut" type="button" title="缩小（显示更多）" aria-label="缩小">−</button>' +
          '<span class="kline-zoom-label num" id="klineZoomLabel">—</span>' +
          '<button class="kline-zoom-btn" id="klineZoomIn" type="button" title="放大（显示更少）" aria-label="放大">+</button>' +
          '<button class="btn btn-mini" id="klineReset" type="button" title="复位视图">复位</button>' +
        '</div>' +
        '<div class="kline-legend" aria-hidden="true">' + maChips + '</div>' +
        '<span class="kline-meta num" id="klineMeta">—</span>' +
      '</div>' +
      '<div class="kline-wrap" id="klineWrap">' +
        '<canvas id="klineCanvas" class="kline-canvas" role="img" tabindex="0" aria-label="个股日K线待加载"></canvas>' +
        '<canvas id="klineAxis" class="kline-axis" aria-hidden="true"></canvas>' +
        '<div class="kline-skel" id="klineSkel"></div>' +
        '<div class="kline-empty" id="klineEmpty" hidden></div>' +
        '<div class="kline-tip" id="klineTip" hidden></div>' +
      '</div>' +
      '<div class="kline-stats" id="klineStats">' +
        '<div class="kstat"><span>区间涨跌</span><b class="num" id="ksRange">—</b></div>' +
        '<div class="kstat"><span>区间振幅</span><b class="num" id="ksAmp">—</b></div>' +
        '<div class="kstat"><span>区间最高</span><b class="num" id="ksHigh">—</b></div>' +
        '<div class="kstat"><span>区间最低</span><b class="num" id="ksLow">—</b></div>' +
        '<div class="kstat"><span>最新量比</span><b class="num" id="ksVol">—</b></div>' +
      '</div>' +
      '<footer class="card-foot kline-foot">' +
        '<span id="klineFoot">' + (IS_CLOUD ? "数据：云端 D1 日线" : "数据：本地行情库") + '</span>' +
        '<span class="foot-right"><span class="kline-hint">滚轮 / 双指缩放 · 拖动平移 · 双击复位</span></span>' +
      '</footer>';
    var events = $("#eventsCard");
    main.insertBefore(sec, events ? events.nextSibling : main.firstChild);
  }

  function klineWindow() {
    var items = state.kline.items || [];
    var n = items.length;
    if (!n) return { items: items, visible: [], start: 0, count: 0, n: 0 };
    var maxCount = Math.min(KLINE_MAX_BARS, n);
    var minCount = Math.min(KLINE_MIN_BARS, n);
    var count = clamp(Math.round(state.kline.view.count || Math.min(KLINE_DEFAULT_BARS, n)), minCount, maxCount);
    var start = clamp(Math.round(state.kline.view.start || 0), 0, Math.max(0, n - count));
    return { items: items, visible: items.slice(start, start + count), start: start, count: count, n: n };
  }

  function setKlineInputValue(symbol) {
    var input = $("#klineSymbol");
    if (input && input.value !== symbol) input.value = symbol;
  }

  function setKlinePresetActive(count) {
    $$("#klineRange .kline-tab").forEach(function (btn) {
      var on = Number(btn.dataset.count) === Number(count);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
  }

  function showKlineLoading() {
    show($("#klineSkel"), true);
    show($("#klineEmpty"), false);
    show($("#klineTip"), false);
    var canvas = $("#klineCanvas");
    if (canvas) canvas.style.visibility = "hidden";
  }

  function showKlineReady() {
    show($("#klineSkel"), false);
    show($("#klineEmpty"), false);
    var canvas = $("#klineCanvas");
    if (canvas) canvas.style.visibility = "visible";
  }

  function showKlineMessage(conf) {
    show($("#klineSkel"), false);
    show($("#klineTip"), false);
    var canvas = $("#klineCanvas");
    if (canvas) canvas.style.visibility = "hidden";
    var box = $("#klineEmpty");
    if (!box) return;
    show(box, true);
    renderInto(box, conf);
    if (conf && conf.retry !== false && /^\d{6}$/.test(state.kline.symbol)) {
      var retry = document.createElement("button");
      retry.type = "button";
      retry.className = "btn kline-retry";
      retry.textContent = "重新加载";
      retry.addEventListener("click", function () {
        void loadKline(state.kline.symbol, { force: true });
      });
      var wrap = box.querySelector(".empty");
      if (wrap) wrap.appendChild(retry);
    }
  }

  function normKlineItems(raw) {
    var items = Array.isArray(raw) ? raw : (raw && Array.isArray(raw.items) ? raw.items : []);
    var out = [];
    items.forEach(function (it) {
      if (!it || typeof it !== "object") return;
      var date = String(it.trade_date || it.date || "").slice(0, 10);
      var close = num(it.close);
      if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !isNum(close)) return;
      out.push({
        date: date,
        open: num(it.open),
        high: num(it.high),
        low: num(it.low),
        close: close,
        volume: num(it.volume) || 0,
        amount: num(it.amount),
        pct: num(it.pct_chg),
        ma5: num(it.ma5),
        ma10: num(it.ma10),
        ma20: num(it.ma20)
      });
    });
    out.sort(function (a, b) { return a.date < b.date ? -1 : a.date > b.date ? 1 : 0; });
    return out.filter(function (it, i, list) { return i === 0 || it.date !== list[i - 1].date; });
  }

  function computeKlineMA(items) {
    [5, 10, 20].forEach(function (win) {
      var key = "ma" + win;
      for (var i = 0; i < items.length; i += 1) {
        if (isNum(items[i][key])) continue;
        if (i + 1 < win) continue;
        var sum = 0;
        var ok = true;
        for (var j = i - win + 1; j <= i; j += 1) {
          if (!isNum(items[j].close)) { ok = false; break; }
          sum += items[j].close;
        }
        if (ok) items[i][key] = sum / win;
      }
    });
    return items;
  }

  function fmtKlineTick(value) {
    if (!isNum(value)) return DASH;
    if (Math.abs(value) >= 1000) return Math.round(value).toLocaleString("en-US");
    return value.toFixed(2);
  }

  function fmtVolumeShou(volume) {
    if (!isNum(volume) || volume <= 0) return DASH;
    var hand = volume / 100;
    if (hand >= 1e8) return (hand / 1e8).toFixed(2) + "亿手";
    if (hand >= 1e4) return (hand / 1e4).toFixed(1) + "万手";
    return Math.round(hand).toLocaleString("en-US") + "手";
  }

  function fmtAmountYi(amount) {
    if (!isNum(amount)) return DASH;
    return (amount / 1e8).toFixed(2) + "亿";
  }

  function pulseNumber(el) {
    if (!el || reduced() || typeof el.animate !== "function") return;
    try {
      el.animate(
        [{ transform: "scale(1)" }, { transform: "scale(1.035)" }, { transform: "scale(1)" }],
        { duration: 260, easing: "cubic-bezier(0.22, 1, 0.36, 1)" }
      );
    } catch (e) { /* 忽略 */ }
  }

  function setKlineText(el, text, cls, pulse) {
    if (!el) return;
    if (el.textContent !== text) {
      el.textContent = text;
      if (pulse === true || (pulse !== false && cls)) pulseNumber(el);
    }
    if (cls !== undefined) {
      el.classList.toggle("up", cls === "up");
      el.classList.toggle("down", cls === "down");
      el.classList.toggle("flat", cls === "flat");
    }
  }

  function renderKlineHeader() {
    var w = klineWindow();
    var sym = state.kline.symbol || "";
    var name = state.kline.name || signalNameFor(sym);
    var titleEl = $("#klSymbolTitle");
    var nameEl = $("#klNameTitle");
    var subEl = $("#klSub");
    var metaEl = $("#klineMeta");
    if (titleEl) titleEl.textContent = sym || "—";
    if (nameEl) {
      nameEl.textContent = name ? " " + name : "";
      nameEl.hidden = !name;
    }
    var lastDate = w.n ? w.items[w.n - 1].date : "";
    if (subEl) {
      subEl.textContent = w.n
        ? "日K · " + w.count + "根 · 至 " + lastDate
        : "日K";
    }
    setKlineText($("#klineZoomLabel"), w.n ? w.count + "根" : "—");
    if (metaEl) {
      metaEl.textContent = w.n
        ? w.n + "根数据 · " + (IS_CLOUD ? "云端 D1" : "本地行情库")
        : (IS_CLOUD ? "云端 D1" : "本地行情库");
    }
    var indexHost = $("#klineIndexTabs");
    if (indexHost) {
      $$(".kline-index-tab", indexHost).forEach(function (btn) {
        btn.setAttribute("aria-selected", btn.dataset.symbol === state.kline.symbol ? "true" : "false");
      });
    }
    var range = $("#klineRange");
    if (range) {
      var active = false;
      $$(".kline-tab", range).forEach(function (btn) {
        var on = w.n && Number(btn.dataset.count) === w.count;
        if (on) active = true;
        btn.setAttribute("aria-selected", on ? "true" : "false");
        btn.disabled = !w.n || w.n < Number(btn.dataset.count);
      });
      $("#klineRange").dataset.preset = active ? String(w.count) : "";
    }
    var lastPrice = w.n ? w.items[w.n - 1].close : undefined;
    var prev = w.n > 1 ? w.items[w.n - 2].close : undefined;
    var pct = isNum(prev) && prev ? lastPrice / prev - 1 : undefined;
    var stateEl = $("#klineCard");
    if (stateEl) {
      stateEl.dataset.trend = isNum(pct) && pct > 0 ? "up" : isNum(pct) && pct < 0 ? "down" : "flat";
    }
  }

  function updateKlineQuote() {
    var w = klineWindow();
    if (!w.n) return;
    var hover = state.kline.hover;
    var useHover = hover >= 0 && hover < w.n;
    var item = useHover ? w.items[hover] : w.items[w.n - 1];
    if (!item) return;
    var prev = w.items[(useHover ? hover : w.n - 1) - 1];
    var pct = isNum(item.pct) ? item.pct / 100 : (isNum(prev) && prev.close ? item.close / prev.close - 1 : undefined);
    var dir = isNum(pct) ? (pct > 0 ? "up" : pct < 0 ? "down" : "flat") : "flat";
    setKlineText($("#kqPrice"), fmtKlineTick(item.close), dir);
    setKlineText($("#kqChg"), isNum(pct) ? (pct > 0 ? "+" : "") + (pct * 100).toFixed(2) + "%" : DASH, dir);
    setKlineText($("#kqDate"), item.date);
    var tag = $("#kqHoverTag");
    if (tag) tag.hidden = !useHover;
    setKlineText($("#kqOpen"), fmtKlineTick(item.open));
    setKlineText($("#kqHigh"), fmtKlineTick(item.high));
    setKlineText($("#kqLow"), fmtKlineTick(item.low));
    setKlineText($("#kqVolume"), fmtVolumeShou(item.volume));
    setKlineText($("#kqAmount"), fmtAmountYi(item.amount));
    var maEl = $("#kqMa");
    if (maEl) {
      maEl.innerHTML = KLINE_MA.map(function (ma) {
        var value = num(item[ma.key]);
        return '<span class="kq-ma-item"><i class="lg-ma ' + ma.key + '"></i>' + ma.label +
          " <b>" + esc(isNum(value) ? fmtKlineTick(value) : DASH) + "</b></span>";
      }).join("");
    }
    var card = $("#klineCard");
    if (card) card.dataset.trend = dir;
  }

  function updateKlineStats() {
    var w = klineWindow();
    if (!w.n) {
      ["#ksRange", "#ksAmp", "#ksHigh", "#ksLow", "#ksVol"].forEach(function (sel) {
        setKlineText($(sel), DASH);
      });
      return;
    }
    var vis = w.visible;
    var first = vis[0];
    var last = vis[vis.length - 1];
    var high = -Infinity;
    var low = Infinity;
    vis.forEach(function (it) {
      if (isNum(it.high)) high = Math.max(high, it.high);
      if (isNum(it.low)) low = Math.min(low, it.low);
    });
    var rangePct = isNum(first.close) && first.close ? last.close / first.close - 1 : undefined;
    var amp = isFinite(low) && low ? (high - low) / low : undefined;
    setKlineText($("#ksRange"), isNum(rangePct) ? (rangePct > 0 ? "+" : "") + (rangePct * 100).toFixed(2) + "%" : DASH,
      isNum(rangePct) ? (rangePct > 0 ? "up" : rangePct < 0 ? "down" : "flat") : undefined, false);
    setKlineText($("#ksAmp"), isNum(amp) ? (amp * 100).toFixed(2) + "%" : DASH);
    setKlineText($("#ksHigh"), isFinite(high) ? fmtKlineTick(high) : DASH);
    setKlineText($("#ksLow"), isFinite(low) ? fmtKlineTick(low) : DASH);
    var latest = w.items[w.n - 1];
    var prevVols = w.items.slice(Math.max(0, w.n - 6), w.n - 1).map(function (it) { return it.volume || 0; });
    var avgVol = prevVols.length ? prevVols.reduce(function (s, v) { return s + v; }, 0) / prevVols.length : 0;
    var ratio = avgVol > 0 && isNum(latest.volume) ? latest.volume / avgVol : undefined;
    setKlineText($("#ksVol"), isNum(ratio) ? ratio.toFixed(2) + (ratio >= 1.5 ? " 放量" : ratio <= 0.6 ? " 缩量" : "") : DASH);
  }

  function scheduleKlineDraw() {
    if (state.kline.raf) return;
    state.kline.raf = requestAnimationFrame(function () {
      state.kline.raf = 0;
      drawKline();
      renderKlineHeader();
      updateKlineQuote();
      updateKlineStats();
    });
  }

  function niceTicks(lo, hi, count) {
    var span = hi - lo;
    if (!(span > 0)) span = Math.abs(hi) || 1;
    var raw = span / Math.max(2, count);
    var mag = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10));
    var norm = raw / mag;
    var step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
    var out = [];
    for (var v = Math.ceil(lo / step) * step; v <= hi + step * 0.001; v += step) {
      out.push(Math.round(v * 1e6) / 1e6);
    }
    return out;
  }

  function drawKlineAxis(axisCanvas, cssH, dpr, ticks, lastTag, hoverTag) {
    if (!axisCanvas) return;
    var axisW = KLINE_AXIS_W;
    axisCanvas.width = Math.round(axisW * dpr);
    axisCanvas.height = Math.round(cssH * dpr);
    axisCanvas.style.width = axisW + "px";
    axisCanvas.style.height = cssH + "px";
    var ctx = axisCanvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, axisW, cssH);
    ctx.font = KLINE_FONT;
    ctx.textBaseline = "middle";
    ctx.textAlign = "right";
    ctx.fillStyle = "#5C6B80";
    var activeTag = hoverTag || lastTag;
    ticks.forEach(function (item) {
      if (activeTag && Math.abs(item.y - activeTag.y) < 10) return;
      ctx.fillText(fmtKlineTick(item.v), axisW - 7, item.y);
    });
    if (!activeTag) return;
    var tagWidth = axisW - 2;
    ctx.fillStyle = activeTag.hover ? "rgba(52, 64, 84, 0.96)" : "rgba(26, 33, 48, 0.96)";
    ctx.fillRect(1, activeTag.y - 9, tagWidth, 18);
    ctx.strokeStyle = activeTag.hover ? "rgba(230, 237, 247, 0.55)" : "rgba(52, 64, 84, 0.9)";
    ctx.lineWidth = 1;
    ctx.strokeRect(1.5, activeTag.y - 8.5, tagWidth - 1, 17);
    ctx.fillStyle = activeTag.color;
    ctx.textAlign = "center";
    ctx.fillText(activeTag.text, axisW / 2, activeTag.y);
  }

  function drawKline() {
    var canvas = $("#klineCanvas");
    var axisCanvas = $("#klineAxis");
    var wrap = $("#klineWrap");
    if (!canvas || !wrap) return;
    var w = klineWindow();
    if (!w.n) return;
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var cssW = Math.max(240, Math.round(wrap.clientWidth - KLINE_AXIS_W));
    var cssH = Math.max(240, Math.round(wrap.clientHeight));
    var needW = Math.round(cssW * dpr);
    var needH = Math.round(cssH * dpr);
    if (canvas.width !== needW || canvas.height !== needH) {
      canvas.width = needW;
      canvas.height = needH;
    }
    canvas.style.width = cssW + "px";
    canvas.style.height = cssH + "px";
    var ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    var vis = w.visible;
    var count = w.count;
    var padT = 10, padL = 8, padR = 8, padB = 20, gap = 8;
    var volH = Math.max(36, Math.round(cssH * 0.18));
    var priceH = cssH - padT - padB - volH - gap;
    var volTop = padT + priceH + gap;
    var plotW = cssW - padL - padR;
    var barW = plotW / Math.max(count, 1);
    var bodyW = Math.max(1, Math.min(barW * 0.64, 13));

    var lo = Infinity, hi = -Infinity;
    vis.forEach(function (it) {
      [it.low, it.high, it.ma5, it.ma10, it.ma20].forEach(function (v) {
        if (!isNum(v)) return;
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      });
    });
    if (!isFinite(lo) || !isFinite(hi)) return;
    var span = hi - lo;
    if (!(span > 0)) span = Math.max(Math.abs(hi) * 0.02, 1);
    lo -= span * 0.06;
    hi += span * 0.06;
    var volMax = 1;
    vis.forEach(function (it) { volMax = Math.max(volMax, it.volume || 0); });

    var x = function (i) { return padL + i * barW + barW / 2; };
    var yPrice = function (v) { return padT + priceH * (1 - (v - lo) / (hi - lo)); };
    var right = cssW - padR;

    // 网格 + 成交量基准
    ctx.lineWidth = 1;
    ctx.strokeStyle = "rgba(230, 237, 247, 0.06)";
    var tickValues = niceTicks(lo, hi, 5);
    var axisTicks = tickValues.map(function (v) { return { v: v, y: Math.round(yPrice(v)) + 0.5 }; });
    axisTicks.forEach(function (item) {
      ctx.beginPath();
      ctx.moveTo(padL, item.y);
      ctx.lineTo(right + 2, item.y);
      ctx.stroke();
    });
    ctx.strokeStyle = "rgba(230, 237, 247, 0.08)";
    ctx.beginPath();
    ctx.moveTo(padL, Math.round(volTop + volH) + 0.5);
    ctx.lineTo(right + 2, Math.round(volTop + volH) + 0.5);
    ctx.stroke();

    var flat = "#8A94A6";
    vis.forEach(function (it, i) {
      var cx = x(i);
      var color = it.close > it.open ? "#FF4D4F" : it.close < it.open ? "#22C55E" : flat;
      var high = isNum(it.high) ? it.high : Math.max(it.open, it.close);
      var low = isNum(it.low) ? it.low : Math.min(it.open, it.close);
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.moveTo(Math.round(cx) + 0.5, yPrice(high));
      ctx.lineTo(Math.round(cx) + 0.5, yPrice(low));
      ctx.stroke();
      var top = yPrice(Math.max(it.open, it.close));
      var bottom = yPrice(Math.min(it.open, it.close));
      ctx.fillRect(cx - bodyW / 2, top, Math.max(1, bodyW), Math.max(1, bottom - top));
      var vh = Math.max(0.8, ((it.volume || 0) / volMax) * (volH - 2));
      ctx.globalAlpha = 0.72;
      ctx.fillRect(cx - bodyW / 2, volTop + volH - vh, Math.max(1, bodyW), vh);
      ctx.globalAlpha = 1;
    });

    KLINE_MA.forEach(function (ma) {
      ctx.strokeStyle = ma.color;
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      var started = false;
      vis.forEach(function (it, i) {
        var v = it[ma.key];
        if (!isNum(v)) { started = false; return; }
        var px = x(i);
        var py = yPrice(v);
        if (!started) { ctx.moveTo(px, py); started = true; }
        else ctx.lineTo(px, py);
      });
      ctx.stroke();
    });

    // 日期刻度
    ctx.fillStyle = "#5C6B80";
    ctx.font = KLINE_FONT;
    ctx.textBaseline = "top";
    var step = Math.max(1, Math.ceil(count / 6));
    var idxs = [];
    for (var i = 0; i < count; i += step) idxs.push(i);
    if (idxs.length && idxs[idxs.length - 1] !== count - 1) {
      if (count - 1 - idxs[idxs.length - 1] >= step * 0.45) idxs.push(count - 1);
      else idxs[idxs.length - 1] = count - 1;
    }
    ctx.fillStyle = "#5C6B80";
    idxs.forEach(function (i) {
      var label = vis[i].date.slice(5);
      if (i === 0) {
        ctx.textAlign = "left";
        ctx.fillText(label, padL, cssH - padB + 5);
      } else if (i === count - 1) {
        ctx.textAlign = "right";
        ctx.fillText(label, right, cssH - padB + 5);
      } else {
        ctx.textAlign = "center";
        ctx.fillText(label, x(i), cssH - padB + 5);
      }
    });
    ctx.textAlign = "left";

    // 十字光标
    var hover = state.kline.hover;
    var hoverVisible = hover >= w.start && hover < w.start + count;
    var hoverTag = null;
    if (hoverVisible) {
      var hi2 = hover - w.start;
      var hx = Math.round(x(hi2)) + 0.5;
      var hoverItem = vis[hi2];
      var hColor = hoverItem.close > hoverItem.open ? "#FF4D4F"
        : hoverItem.close < hoverItem.open ? "#22C55E" : flat;
      ctx.save();
      ctx.setLineDash([3, 3]);
      ctx.strokeStyle = "rgba(230, 237, 247, 0.32)";
      ctx.beginPath();
      ctx.moveTo(hx, padT);
      ctx.lineTo(hx, volTop + volH);
      ctx.stroke();
      var hy = yPrice(hoverItem.close);
      ctx.beginPath();
      ctx.moveTo(padL, hy);
      ctx.lineTo(right + 2, hy);
      ctx.stroke();
      ctx.restore();
      ctx.fillStyle = "#11161F";
      ctx.strokeStyle = hColor;
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.arc(hx, hy, 3, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      hoverTag = { y: hy, color: hColor, text: hoverItem.close.toFixed(2), hover: true };
    }

    // 最新可见价标签
    var last = vis[count - 1];
    var lastColor = last.close > last.open ? "#FF4D4F" : last.close < last.open ? "#22C55E" : flat;
    var lastTag = { y: yPrice(last.close), color: lastColor, text: last.close.toFixed(2) };
    drawKlineAxis(axisCanvas, cssH, dpr, axisTicks, lastTag, hoverTag);

    canvas.setAttribute(
      "aria-label",
      (state.kline.symbol || "") + " 日K线，显示 " + count + " 根，" + vis[0].date + " 至 " + last.date +
      "，最新收盘 " + last.close.toFixed(2) + "。滚轮或双指缩放，拖动平移，双击复位。"
    );
    if (hoverVisible) positionKlineTip(hover);
    else hideKlineTip();
  }

  function positionKlineTip(dataIndex) {
    var tip = $("#klineTip");
    var wrap = $("#klineWrap");
    if (!tip || !wrap) return;
    var w = klineWindow();
    if (dataIndex < w.start || dataIndex >= w.start + w.count) { hideKlineTip(); return; }
    var item = w.items[dataIndex];
    if (!item) return;
    var prev = w.items[dataIndex - 1];
    var pct = isNum(item.pct) ? item.pct / 100 : (isNum(prev) && prev.close ? item.close / prev.close - 1 : undefined);
    var cls = isNum(pct) ? (pct > 0 ? "up" : pct < 0 ? "down" : "flat") : "flat";
    tip.innerHTML =
      '<div class="kt-row"><span>日期</span><b>' + esc(item.date) + "</b></div>" +
      '<div class="kt-row"><span>涨跌</span><b class="' + cls + '">' + esc(isNum(pct) ? (pct > 0 ? "+" : "") + (pct * 100).toFixed(2) + "%" : DASH) + "</b></div>" +
      '<div class="kt-row"><span>开盘/收盘</span><b>' + esc(fmtKlineTick(item.open)) + " / " + esc(fmtKlineTick(item.close)) + "</b></div>" +
      '<div class="kt-row"><span>最高/最低</span><b>' + esc(fmtKlineTick(item.high)) + " / " + esc(fmtKlineTick(item.low)) + "</b></div>" +
      '<div class="kt-row"><span>成交量</span><b>' + esc(fmtVolumeShou(item.volume)) + "</b></div>" +
      '<div class="kt-row"><span>MA5/10/20</span><b>' +
        esc(fmtKlineTick(item.ma5)) + " / " + esc(fmtKlineTick(item.ma10)) + " / " + esc(fmtKlineTick(item.ma20)) + "</b></div>";
    show(tip, true);
    var canvas = $("#klineCanvas");
    var canvasRect = canvas ? canvas.getBoundingClientRect() : { left: 0, width: 0 };
    var wrapRect = wrap.getBoundingClientRect();
    var cssW = Math.max(240, wrap.clientWidth - KLINE_AXIS_W);
    var plotW = cssW - 16;
    var barW = plotW / Math.max(w.count, 1);
    var px = 8 + (dataIndex - w.start) * barW + barW / 2;
    var tw = tip.offsetWidth || 196;
    var th = tip.offsetHeight || 108;
    var tx = px + 14;
    if (tx + tw > cssW - 2) tx = px - tw - 14;
    tx = Math.max(4, Math.min(tx, Math.max(4, cssW - tw - 4)));
    var ty = 14;
    if (state.kline.pointer && wrapRect) {
      ty = clamp(state.kline.pointer.y - wrapRect.top - th - 14, 6, Math.max(6, wrap.clientHeight - th - 6));
    }
    tip.style.transform = "translate3d(" + Math.round(tx) + "px," + Math.round(ty) + "px,0)";
  }

  function hideKlineTip() {
    show($("#klineTip"), false);
  }

  function applyKlineView(start, count) {
    var w = klineWindow();
    if (!w.n) return;
    var maxCount = Math.min(KLINE_MAX_BARS, w.n);
    var minCount = Math.min(KLINE_MIN_BARS, w.n);
    var nextCount = clamp(Math.round(count), minCount, maxCount);
    var nextStart = clamp(Math.round(start), 0, Math.max(0, w.n - nextCount));
    if (nextCount === state.kline.view.count && nextStart === state.kline.view.start) return;
    state.kline.view = { start: nextStart, count: nextCount };
    if (state.kline.hover >= 0 && (state.kline.hover < nextStart || state.kline.hover >= nextStart + nextCount)) {
      state.kline.hover = -1;
    }
    scheduleKlineDraw();
  }

  function zoomKline(factor, anchorRatio) {
    var w = klineWindow();
    if (!w.n) return;
    var maxCount = Math.min(KLINE_MAX_BARS, w.n);
    var minCount = Math.min(KLINE_MIN_BARS, w.n);
    var ratio = clamp(isNum(anchorRatio) ? anchorRatio : 0.5, 0, 1);
    var anchorIndex = w.start + (w.count - 1) * ratio;
    var nextCount = clamp(Math.round(w.count * factor), minCount, maxCount);
    if (nextCount === w.count) return;
    var nextStart = Math.round(anchorIndex - (nextCount - 1) * ratio);
    applyKlineView(nextStart, nextCount);
  }

  function panKline(bars) {
    var w = klineWindow();
    if (!w.n || !bars) return;
    applyKlineView(w.start + bars, w.count);
  }

  function resetKlineView() {
    var w = klineWindow();
    if (!w.n) return;
    var count = Math.min(KLINE_DEFAULT_BARS, w.n);
    state.kline.hover = -1;
    hideKlineTip();
    applyKlineView(w.n - count, count);
  }

  function setKlinePreset(count) {
    var w = klineWindow();
    if (!w.n) return;
    var target = Math.min(Number(count) || KLINE_DEFAULT_BARS, w.n, KLINE_MAX_BARS);
    state.kline.hover = -1;
    hideKlineTip();
    applyKlineView(w.n - target, target);
  }

  function klinePlotRatio(ev) {
    var canvas = $("#klineCanvas");
    if (!canvas) return 0.5;
    var rect = canvas.getBoundingClientRect();
    var wrap = $("#klineWrap");
    var wrapWidth = wrap ? Math.max(240, wrap.clientWidth - KLINE_AXIS_W) : rect.width;
    var localX = ev.clientX - rect.left;
    return clamp((localX - 8) / Math.max(1, wrapWidth - 16), 0, 1);
  }

  function klineIndexAtX(clientX) {
    var canvas = $("#klineCanvas");
    if (!canvas) return -1;
    var rect = canvas.getBoundingClientRect();
    var w = klineWindow();
    if (!w.n) return -1;
    var cssW = Math.max(240, rect.width);
    var plotW = cssW - 16;
    var barW = plotW / Math.max(w.count, 1);
    var idx = Math.floor((clientX - rect.left - 8) / barW);
    if (idx < 0 || idx >= w.count) return -1;
    return w.start + idx;
  }

  function applyKlineItems(symbol, items, options) {
    var opts = options || {};
    state.kline.symbol = symbol;
    state.kline.name = signalNameFor(symbol);
    state.kline.items = items;
    state.kline.cache.set(symbol, items);
    if (state.kline.cache.size > 8) {
      var firstKey = state.kline.cache.keys().next().value;
      state.kline.cache.delete(firstKey);
    }
    state.kline.hover = -1;
    var count = Math.min(KLINE_DEFAULT_BARS, items.length);
    state.kline.view = { start: Math.max(0, items.length - count), count: count };
    rememberKlineSymbol(symbol);
    if (opts.auto !== true) state.kline.auto = false;
    setKlineInputValue(symbol);
    showKlineReady();
    renderKlineHeader();
    updateKlineQuote();
    updateKlineStats();
    scheduleKlineDraw();
  }

  function loadKline(symbol, options) {
    var opts = options || {};
    var sym = String(symbol || "").trim().toLowerCase();
    if (!/^(?:\d{6}|(?:sh|sz)\d{6})$/.test(sym)) {
      showKlineMessage({ title: "代码格式不正确", message: "请输入 6 位 A 股代码，或上证/深证指数代码（如 sh000001、sz399001）。", retry: false, icon: "i-warning" });
      return Promise.resolve();
    }
    if (opts.auto !== true) state.kline.auto = false;
    state.kline.symbol = sym;
    state.kline.name = signalNameFor(sym);
    setKlineInputValue(sym);
    renderKlineHeader();
    var cached = state.kline.cache.get(sym);
    if (cached && !opts.force) {
      applyKlineItems(sym, cached, { auto: opts.auto });
      return Promise.resolve();
    }
    var reqId = ++state.kline.req;
    state.kline.loading = true;
    showKlineLoading();
    var sub = $("#klSub");
    if (sub) sub.textContent = "日K · 加载中…";
    return getJSON(EP.kline + "?symbol=" + encodeURIComponent(sym) + "&days=" + KLINE_LOAD_DAYS).then(function (r) {
      if (reqId !== state.kline.req) return;
      state.kline.loading = false;
      if (r.challenge) {
        noteChallenge();
        showKlineMessage({ title: "需要完成访问验证", message: "验证通过后会自动加载 K 线。", retry: false, icon: "i-lock" });
        return;
      }
      if (r.rateLimited) {
        startRateLimit(r.retryAfter);
        showKlineMessage({ title: "请求过于频繁", message: "稍后会自动恢复，请稍后再试。", retry: true, icon: "i-warning" });
        return;
      }
      if (r.error) {
        if (r.error.status === 404) {
          showKlineMessage({
            title: "暂无该代码的 K 线",
            message: IS_CLOUD ? "这个代码暂时没有可展示的日线数据，可以换一个代码试试。" : "本地行情库暂未覆盖这个代码，可以换一个代码试试。",
            note: "换代码不会影响其它面板的数据。",
            retry: true,
            icon: "i-chart"
          });
        } else {
          showKlineMessage({ title: "K 线加载失败", message: "行情服务暂时不可用，请稍后重试。", retry: true, icon: "i-warning" });
        }
        return;
      }
      var items = computeKlineMA(normKlineItems(r.data));
      if (!items.length) {
        showKlineMessage({ title: "暂无 K 线数据", message: "该代码暂时没有可展示的日线数据。", retry: true, icon: "i-chart" });
        return;
      }
      applyKlineItems(sym, items, { auto: opts.auto });
    });
  }

  function initKlineInteractions() {
    var input = $("#klineSymbol");
    var go = $("#klineGo");
    var range = $("#klineRange");
    var canvas = $("#klineCanvas");
    if (!input || !go) return;
    var submit = function () {
      var code = String(input.value || "").trim();
      loadKline(code, { force: false });
    };
    go.addEventListener("click", submit);
    input.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") { ev.preventDefault(); submit(); }
    });
    if (range) {
      range.addEventListener("click", function (ev) {
        var btn = ev.target.closest ? ev.target.closest(".kline-tab") : null;
        if (!btn || btn.disabled) return;
        setKlinePreset(Number(btn.dataset.count));
      });
      range.addEventListener("keydown", function (ev) {
        if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
        var tabs = $$(".kline-tab", range);
        var idx = tabs.findIndex(function (t) { return t.getAttribute("aria-selected") === "true"; });
        var next = ev.key === "ArrowRight" ? (idx + 1) % tabs.length : (idx - 1 + tabs.length) % tabs.length;
        tabs[next].focus();
        tabs[next].click();
        ev.preventDefault();
      });
    }
    var indexHost = $("#klineIndexTabs");
    if (indexHost) {
      indexHost.addEventListener("click", function (ev) {
        var btn = ev.target.closest ? ev.target.closest(".kline-index-tab") : null;
        if (!btn || !btn.dataset.symbol) return;
        state.kline.auto = false;
        loadKline(btn.dataset.symbol, { force: false });
      });
    }
    var zoomIn = $("#klineZoomIn");
    var zoomOut = $("#klineZoomOut");
    var reset = $("#klineReset");
    if (zoomIn) zoomIn.addEventListener("click", function () { zoomKline(1 / 1.35, 0.5); });
    if (zoomOut) zoomOut.addEventListener("click", function () { zoomKline(1.35, 0.5); });
    if (reset) reset.addEventListener("click", resetKlineView);

    if (!canvas) return;
    var pointers = new Map();
    var drag = null;
    var pinch = null;

    canvas.addEventListener("wheel", function (ev) {
      if (!state.kline.items.length) return;
      ev.preventDefault();
      var factor = ev.deltaY > 0 ? 1.16 : 1 / 1.16;
      zoomKline(factor, klinePlotRatio(ev));
    }, { passive: false });

    canvas.addEventListener("dblclick", function (ev) {
      ev.preventDefault();
      resetKlineView();
    });

    canvas.addEventListener("pointerdown", function (ev) {
      if (!state.kline.items.length) return;
      canvas.focus({ preventScroll: true });
      try { canvas.setPointerCapture(ev.pointerId); } catch (e) { /* 忽略 */ }
      pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
      if (pointers.size === 2) {
        var pts = Array.from(pointers.values());
        var dist = Math.abs(pts[0].x - pts[1].x) || 1;
        var mid = (pts[0].x + pts[1].x) / 2;
        var evLike = { clientX: mid };
        var w = klineWindow();
        pinch = {
          dist0: dist,
          count0: w.count,
          start0: w.start,
          ratio0: klinePlotRatio(evLike)
        };
        drag = null;
      } else if (pointers.size === 1) {
        var w1 = klineWindow();
        drag = {
          x0: ev.clientX,
          start0: w1.start,
          barW: Math.max(1, (Math.max(240, canvas.clientWidth) - 16) / Math.max(1, w1.count)),
          moved: false,
          touch: ev.pointerType === "touch"
        };
      }
    });

    canvas.addEventListener("pointermove", function (ev) {
      if (!state.kline.items.length) return;
      if (pointers.has(ev.pointerId)) pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
      if (pointers.size === 2 && pinch) {
        var pts = Array.from(pointers.values());
        var dist = Math.abs(pts[0].x - pts[1].x) || 1;
        var w0 = klineWindow();
        var maxCount = Math.min(KLINE_MAX_BARS, w0.n);
        var minCount = Math.min(KLINE_MIN_BARS, w0.n);
        var ratio = dist / pinch.dist0;
        var nextCount = clamp(Math.round(pinch.count0 / Math.max(ratio, 0.2)), minCount, maxCount);
        var anchorIndex = pinch.start0 + (pinch.count0 - 1) * pinch.ratio0;
        var nextStart = Math.round(anchorIndex - (nextCount - 1) * pinch.ratio0);
        applyKlineView(nextStart, nextCount);
        return;
      }
      if (drag) {
        var dx = ev.clientX - drag.x0;
        if (Math.abs(dx) > 2) drag.moved = true;
        if (drag.moved) {
          var bars = Math.round(dx / drag.barW);
          applyKlineView(drag.start0 - bars, state.kline.view.count);
          state.kline.hover = -1;
          hideKlineTip();
          scheduleKlineDraw();
          return;
        }
      }
      if (ev.pointerType === "mouse") {
        state.kline.pointer = { x: ev.clientX, y: ev.clientY };
        var idx = klineIndexAtX(ev.clientX);
        if (idx >= 0 && idx !== state.kline.hover) {
          state.kline.hover = idx;
          scheduleKlineDraw();
        }
        canvas.style.cursor = "crosshair";
      }
    });

    function endPointer(ev) {
      var wasPointer = pointers.has(ev.pointerId);
      pointers.delete(ev.pointerId);
      try { canvas.releasePointerCapture(ev.pointerId); } catch (e) { /* 忽略 */ }
      if (pointers.size < 2) pinch = null;
      if (wasPointer && drag && !drag.moved && ev.type === "pointerup") {
        var idx = klineIndexAtX(ev.clientX);
        if (idx >= 0) {
          state.kline.hover = idx;
          scheduleKlineDraw();
        }
      }
      if (pointers.size === 0) drag = null;
    }
    canvas.addEventListener("pointerup", endPointer);
    canvas.addEventListener("pointercancel", endPointer);
    canvas.addEventListener("pointerleave", function (ev) {
      if (pointers.size) return;
      if (ev.pointerType !== "mouse") return;
      if (state.kline.hover < 0) return;
      state.kline.hover = -1;
      hideKlineTip();
      scheduleKlineDraw();
    });

    canvas.addEventListener("keydown", function (ev) {
      var handled = ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "+", "=", "-", "_", "0", "Escape"];
      if (handled.indexOf(ev.key) === -1) return;
      var w = klineWindow();
      if (!w.n && ev.key !== "Escape") return;
      ev.preventDefault();
      if (ev.key === "ArrowLeft" || ev.key === "ArrowRight") {
        var cur = state.kline.hover >= 0 ? state.kline.hover : w.start + w.count - 1;
        var next = clamp(cur + (ev.key === "ArrowRight" ? 1 : -1), w.start, w.start + w.count - 1);
        if (next === w.start && ev.key === "ArrowLeft" && w.start > 0) {
          applyKlineView(w.start - 1, w.count);
        } else if (next === w.start + w.count - 1 && ev.key === "ArrowRight" && w.start + w.count < w.n) {
          applyKlineView(w.start + 1, w.count);
        }
        state.kline.hover = next;
        scheduleKlineDraw();
      } else if (ev.key === "+" || ev.key === "=") {
        zoomKline(1 / 1.35, 0.5);
      } else if (ev.key === "-" || ev.key === "_") {
        zoomKline(1.35, 0.5);
      } else if (ev.key === "0") {
        resetKlineView();
      } else if (ev.key === "Escape") {
        state.kline.hover = -1;
        hideKlineTip();
        scheduleKlineDraw();
      }
    });

    var wrap = $("#klineWrap");
    if (wrap && window.ResizeObserver) {
      var ro = new ResizeObserver(function () {
        if (state.kline.items.length) scheduleKlineDraw();
      });
      ro.observe(wrap);
    }
  }

  function initSignalRowKline() {
    var body = $("#signalsBody");
    if (!body) return;
    body.addEventListener("click", function (ev) {
      var tr = ev.target.closest ? ev.target.closest("tr[data-symbol]") : null;
      if (!tr) return;
      var symbol = tr.dataset.symbol;
      if (!/^\d{6}$/.test(symbol)) return;
      state.kline.auto = false;
      loadKline(symbol, { force: false });
      announce("K 线切换到 " + symbol);
    });
  }


  /* ============================== 大盘状态面板 ============================== */
  function createRegimePanel() {
    var main = $("#main");
    if (!main || $("#regimeCard")) return;
    var sec = document.createElement("section");
    sec.id = "regimeCard";
    sec.className = "card span-4";
    sec.setAttribute("aria-labelledby", "rgTitle");
    sec.innerHTML =
      '<header class="card-head">' +
        '<h2 class="card-title" id="rgTitle">大盘状态<span class="card-sub" id="rgSub">情绪 · 主线 · 仓位</span></h2>' +
        '<div class="card-meta"><span id="rgAsOf">—</span></div>' +
      '</header>' +
      '<div class="regime-body" id="rgBody">' +
        '<div class="rg-state">' +
          '<div class="rg-state-main"><b class="rg-state-label" id="rgStateLabel">—</b><span class="rg-score num" id="rgScore">—</span><span class="rg-score-unit">分</span></div>' +
          '<div class="rg-track"><i id="rgFill"></i></div>' +
          '<div class="rg-state-meta muted" id="rgStateMeta">—</div>' +
        '</div>' +
      '</div>' +
      '<footer class="card-foot"><span>研究用途，不构成投资建议</span><span class="foot-right" id="rgFoot">—</span></footer>';
    var kline = $("#klineCard");
    if (kline) main.insertBefore(sec, kline.nextSibling);
    else {
      var events = $("#eventsCard");
      main.insertBefore(sec, events ? events.nextSibling : main.firstChild);
    }
  }

  function regimeEmptyConf() {
    return IS_CLOUD ? {
      title: "暂无大盘状态快照",
      message: "云端还没有收到大盘状态推送。",
      note: "本地运行 python -m quant.cli export --target cloudflare 推送。",
      icon: "i-warning"
    } : {
      title: "暂无大盘状态快照",
      message: "还没有生成 data/reports/regime.json。",
      command: "python scripts/regime_report.py",
      note: "生成后刷新即可看到状态/情绪/主线/仓位建议。",
      icon: "i-warning"
    };
  }

  function loadRegime() {
    return getJSON(EP.regime).then(function (r) {
      if (r.challenge) { noteChallenge(); return { kind: "challenge" }; }
      if (r.rateLimited) { startRateLimit(r.retryAfter); return { kind: "ratelimit" }; }
      if (r.error) {
        state.regime = null;
        state.regimeUpdatedAt = null;
        renderRegime();
        return { kind: r.error.status === 404 ? "empty" : "error" };
      }
      var data = r.data || {};
      var snapshot = data.snapshot && typeof data.snapshot === "object" ? data.snapshot : data;
      state.regime = snapshot && snapshot.market_state ? snapshot : null;
      state.regimeUpdatedAt = data.updated_at || (snapshot && snapshot.asof) || null;
      renderRegime();
      return { kind: state.regime ? "ok" : "empty" };
    });
  }

  function regimeTone(score) {
    if (!isNum(score)) return "flat";
    if (score < 30) return "down";
    if (score > 70) return "up";
    return "flat";
  }

  function renderRegime() {
    var body = $("#rgBody");
    if (!body) return;
    var rg = state.regime;
    var empty = $("#rgEmpty");
    var title = $("#rgTitle");
    if (!rg) {
      var emptyBox = empty;
      if (!emptyBox) {
        emptyBox = document.createElement("div");
        emptyBox.id = "rgEmpty";
        emptyBox.className = "card-empty";
        body.replaceChildren(emptyBox);
      }
      show(emptyBox, true);
      renderInto(emptyBox, regimeEmptyConf());
      if (title) title.innerHTML = '大盘状态<span class="card-sub">情绪 · 主线 · 仓位</span>';
      setText($("#rgAsOf"), "—");
      setText($("#rgFoot"), "研究用途，不构成投资建议");
      return;
    }
    if (empty) { empty.remove(); }
    if (!body.firstElementChild || body.firstElementChild.id === "rgEmpty") {
      body.innerHTML = '<div class="rg-state">' +
        '<div class="rg-state-main"><b class="rg-state-label" id="rgStateLabel">—</b><span class="rg-score num" id="rgScore">—</span><span class="rg-score-unit">分</span></div>' +
        '<div class="rg-track"><i id="rgFill"></i></div>' +
        '<div class="rg-state-meta muted" id="rgStateMeta">—</div>' +
      '</div>' +
      '<div class="rg-cols">' +
        '<div class="rg-col"><span class="muted">情绪</span><b class="num" id="rgEmotion">—</b><span class="muted" id="rgEmotionMeta">—</span></div>' +
        '<div class="rg-col"><span class="muted" id="rgIndexName">沪深300</span><b class="num" id="rgIndex">—</b><span class="muted" id="rgIndexMeta">—</span></div>' +
      '</div>' +
      '<div class="rg-block"><div class="col-head"><span>主线 Top5</span><span class="col-meta">当日 / 5日 / 净流入(亿)</span></div>' +
        '<div class="rg-leaders" id="rgLeaders"><div class="rg-empty">暂无主线数据</div></div></div>' +
      '<div class="rg-block rg-position"><div class="col-head"><span>仓位建议</span><span class="col-meta" id="rgPosLabel">—</span></div>' +
        '<div class="rg-pos-row"><b class="rg-pos-range" id="rgPosRange">—</b><span class="rg-pos-note">研究用途，不构成投资建议</span></div>' +
        '<p class="rg-rationale" id="rgRationale">—</p><ul class="rg-triggers" id="rgTriggers"></ul></div>' +
      '<details class="rg-why" id="rgWhy" hidden><summary id="rgWhySummary">判断依据</summary><ul id="rgWhyList"></ul></details>';
    }

    var ms = rg.market_state || {};
    var score = num(ms.score);
    var label = ms.label || "—";
    if (title) title.innerHTML = "大盘状态" + '<span class="card-sub">' + esc(label) + "</span>";
    setText($("#rgStateLabel"), label);
    tweenNumber($("#rgScore"), score, function (v) { return fmtFixed(v, 1); });
    var fill = $("#rgFill");
    if (fill) fill.style.width = clamp(score || 0, 0, 100) + "%";
    var parts = [];
    if (isNum(ms.score_parts && ms.score_parts.trend)) parts.push("趋势 " + fmtFixed(ms.score_parts.trend, 1));
    if (isNum(ms.score_parts && ms.score_parts.emotion)) parts.push("情绪 " + fmtFixed(ms.score_parts.emotion, 1));
    if (isNum(ms.score_parts && ms.score_parts.volume)) parts.push("量能 " + fmtFixed(ms.score_parts.volume, 1));
    setText($("#rgStateMeta"), parts.length ? "状态分构成 " + parts.join(" · ") : "状态分 0–100");

    var emo = rg.emotion || {};
    var emoEl = $("#rgEmotion");
    if (emoEl) {
      emoEl.textContent = isNum(emo.score) ? fmtFixed(emo.score, 1) : DASH;
      emoEl.className = "num " + (emo.state === "ice" ? "down" : emo.state === "hot" ? "up" : "");
    }
    var emoMeta = [];
    if (emo.label) emoMeta.push(emo.label);
    if (isNum(emo.breadth)) emoMeta.push("宽度 " + fmtPct(emo.breadth));
    if (isNum(emo.limit_up) || isNum(emo.limit_down)) {
      emoMeta.push("涨停 " + fmtInt(emo.limit_up) + " / 跌停 " + fmtInt(emo.limit_down));
    }
    setText($("#rgEmotionMeta"), emoMeta.join(" · ") || DASH);

    var trend = rg.trend || {};
    setText($("#rgIndexName"), trend.index || "沪深300");
    var idxEl = $("#rgIndex");
    if (idxEl) {
      idxEl.textContent = isNum(trend.close) ? fmtPrice(trend.close) : DASH;
      idxEl.className = "num " + (isNum(trend.ret5) && trend.ret5 > 0 ? "up" : isNum(trend.ret5) && trend.ret5 < 0 ? "down" : "");
    }
    var idxMeta = [];
    if (isNum(trend.ret5)) idxMeta.push("5日 " + fmtSignedPct(trend.ret5 / 100));
    if (isNum(trend.ret20)) idxMeta.push("20日 " + fmtSignedPct(trend.ret20 / 100));
    if (trend.ma_state) idxMeta.push(trend.ma_state);
    setText($("#rgIndexMeta"), idxMeta.join(" · ") || DASH);

    var leaders = arr(rg.leaders).slice(0, 5);
    var box = $("#rgLeaders");
    if (box) {
      if (!leaders.length) {
        box.innerHTML = '<div class="rg-empty">暂无主线数据</div>';
      } else {
        box.innerHTML = leaders.map(function (item) {
          var d1 = num(item.pct_1d);
          var d5 = num(item.pct_5d);
          var inflow = num(item.net_inflow);
          var kind = item.kind === "concept" ? "概念" : item.kind === "industry" ? "行业" : "";
          return '<div class="rg-leader" title="' + esc(item.tag || "") + '">' +
            '<span class="rg-leader-name">' + esc(item.name || "—") +
              (kind ? '<i class="rg-kind">' + kind + "</i>" : "") + "</span>" +
            '<span class="rg-leader-cell num ' + (isNum(d1) && d1 > 0 ? "up" : isNum(d1) && d1 < 0 ? "down" : "") + '">' +
              esc(isNum(d1) ? (d1 > 0 ? "+" : "") + fmtFixed(d1, 2) + "%" : DASH) + "</span>" +
            '<span class="rg-leader-cell num ' + (isNum(d5) && d5 > 0 ? "up" : isNum(d5) && d5 < 0 ? "down" : "") + '">' +
              esc(isNum(d5) ? (d5 > 0 ? "+" : "") + fmtFixed(d5, 2) + "%" : DASH) + "</span>" +
            '<span class="rg-leader-cell num ' + (isNum(inflow) && inflow > 0 ? "up" : isNum(inflow) && inflow < 0 ? "down" : "") + '">' +
              esc(isNum(inflow) ? (inflow > 0 ? "+" : "") + fmtFixed(inflow, 1) : DASH) + "</span>" +
          "</div>";
        }).join("");
      }
    }

    var pos = rg.position || {};
    setText($("#rgPosLabel"), pos.label || "仓位建议");
    setText($("#rgPosRange"), pos.range || DASH);
    setText($("#rgRationale"), pos.rationale || "—");
    var triggers = arr(pos.triggers);
    var triggerBox = $("#rgTriggers");
    if (triggerBox) {
      triggerBox.innerHTML = triggers.slice(0, 3).map(function (t) {
        return "<li>" + esc(t) + "</li>";
      }).join("");
    }

    var reasons = arr(ms.reasons);
    var why = $("#rgWhy");
    if (why) {
      why.hidden = reasons.length === 0;
      setText($("#rgWhySummary"), "判断依据（" + reasons.length + "）");
      var whyList = $("#rgWhyList");
      if (whyList) whyList.innerHTML = reasons.map(function (r) { return "<li>" + esc(r) + "</li>"; }).join("");
    }

    var asof = rg.trade_date || state.regimeUpdatedAt || rg.asof || "";
    setText($("#rgAsOf"), asof ? String(asof).slice(0, 10) : DASH);
    var quality = rg.data_quality || {};
    var footParts = [];
    if (rg.trade_date) footParts.push("交易日 " + rg.trade_date);
    if (rg.status && rg.status !== "ok") footParts.push("数据状态 " + rg.status);
    if (arr(quality.warnings).length) footParts.push("警告 " + quality.warnings.length + " 条");
    setText($("#rgFoot"), footParts.join(" · ") || "研究用途，不构成投资建议");
    renderPanorama();
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
      if (state.kline.auto && latest && /^\d{6}$/.test(latest.symbol)) {
        state.kline.auto = false;
        if (latest.symbol !== state.kline.symbol) void loadKline(latest.symbol, { auto: true });
      } else if (state.kline.auto) {
        state.kline.auto = false;
      }
      if (state.kline.items.length) renderKlineHeader();
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
      var paperPayload = (r.data && r.data.snapshot) ? r.data.snapshot : r.data;
      state.paper = normPaper(paperPayload);
      if (IS_CLOUD && state.paper && state.paper.curve && state.paper.curve.length) {
        state.equity = state.paper.curve.map(function (pt) {
          return { ts: pt.ts, equity: pt.equity, cash: pt.cash, marketValue: pt.market_value };
        });
        renderEquity();
      }
      state.lastOkAt = Date.now();
      renderPaper();
      renderStatus();
      return { kind: state.paper ? "ok" : "empty" };
    });
  }

  function loadEquity() {
    if (IS_CLOUD) {
      state.equity = [];
      renderEquity();
      return Promise.resolve({ kind: "empty" });
    }
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
            note: "回测数据生成后自动展示指标与校准图。",
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
      var btPayload = (r.data && r.data.snapshot) ? r.data.snapshot : r.data;
      state.backtest = normBacktest(btPayload);
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
    return Promise.all([loadSignals(), loadMarket(), loadPaper(), loadEquity(), loadBacktest(), loadEvents(), loadRegime(), loadSummary()]).then(function (results) {
      state.inflight = false;
      if (btn) btn.classList.remove("is-loading");
      if (reason === "manual" && state.kline.symbol) {
        void loadKline(state.kline.symbol, { force: true });
      }
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
      if (state.marketPollTimer) { clearTimeout(state.marketPollTimer); state.marketPollTimer = null; }
      if (state.clockTimer) { clearInterval(state.clockTimer); state.clockTimer = null; }
      return;
    }
    if (state.gate.blocked) return;
    if (state.rateLimitUntil <= Date.now()) void refreshAll("visible");
    if (IS_CLOUD) resumeStream();
    else startPolling();
    startRealtime();
    void loadIntraday(state.intraday.code, { force: true });
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
    initGlobalSearch();
    initKlineInteractions();
    initSignalRowKline();
    var todayTabs = $("#tdTabs");
    if (todayTabs) {
      todayTabs.addEventListener("click", function (ev) {
        var btn = ev.target.closest ? ev.target.closest(".td-tab") : null;
        if (!btn || !btn.dataset.code) return;
        loadIntraday(btn.dataset.code, { force: false });
      });
    }
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
    createIndexStrip();
    createEventsPanel();
    createTodayPanel();
    createSummaryPanel();
    createKlinePanel();
    createRegimePanel();
    ensureBacktestExtras();
    reorderWorkbenchPanels();
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
    startRealtime();
    void loadIntraday(state.intraday.code, { force: true });
    void loadMarketHistory();
    void loadSummary();
    var remembered = rememberedKlineSymbol();
    void loadKline(remembered || "600519", { auto: !remembered });
  }

  /** 桌面/手机统一优先级：市场整体 → K线 → 大盘状态 → 事件 → 其余面板。 */
  function reorderWorkbenchPanels() {
    var main = $("#main");
    if (!main) return;
    var title = $("#signalTitle");
    var anchor = title && title.closest ? title.closest("section") : null;
    if (!anchor) return;
    ["marketCard", "todayCard", "klineCard", "regimeCard", "summaryCard", "eventsCard"].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) main.insertBefore(el, anchor);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
