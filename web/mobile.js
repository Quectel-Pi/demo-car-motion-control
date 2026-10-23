/* 前端异常上报：任何 JS 报错都会 POST 到板子的 /api/client_log，
 * 落到板端 ~/wheel-client-errors.log，方便定位"页面能开但操作没反应"。 */
(function () {
  if (window.__wcErrBeacon) return;
  window.__wcErrBeacon = true;
  const send = (kind, detail) => {
    try {
      fetch('/api/client_log', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          kind: kind,
          page: location.pathname,
          ua: (navigator.userAgent || '').slice(0, 140),
          detail: detail,
        }),
        keepalive: true,
      }).catch(() => {});
    } catch (_) { /* ignore */ }
  };
  window.addEventListener('error', (e) => {
    send('error', {
      msg: e.message,
      src: ((e.filename || '').split('/').pop() || ''),
      line: e.lineno, col: e.colno,
      stack: ((e.error && e.error.stack) || '').slice(0, 600),
    });
  });
  window.addEventListener('unhandledrejection', (e) => {
    const r = e.reason;
    send('rejection', {
      msg: String((r && r.message) || r).slice(0, 300),
      stack: String((r && r.stack) || '').split('\n').slice(0, 6).join(' | ').slice(0, 700),
    });
  });
})();

/* 手机精简版控制页（/m.html，手机访问 / 时自动进入）
 * 只保留：状态、3D、摇杆、速度/自转、停车、急停；
 * 单轮点检 / 演示 / 日志收进「更多」，默认折叠。
 * 接口与完整版完全一致：/api/status、/api/target、/api/raw、/api/stop、/api/estop、/api/demo/*。
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);

  /* 资源加载失败时给一条**可见**提示（弱网下最常见的现象就是某个 js/css 没下来，
   * 页面看起来"加载不完全"却没有任何报错）。 */
  function showLoadError(url) {
    let bar = document.getElementById('load-err');
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'load-err';
      bar.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:99;padding:10px 14px;'
        + 'background:#e5484d;color:#fff;font:13px/1.5 system-ui,sans-serif;display:flex;gap:10px;align-items:center';
      document.body.appendChild(bar);
    }
    bar.innerHTML = '<span style="flex:1">Failed to load: ' + String(url).split('/').pop()
      + ' (flaky network - tap Reload)</span>';
    const btn = document.createElement('button');
    btn.textContent = 'Reload';
    btn.style.cssText = 'border:0;border-radius:8px;padding:6px 12px;font-weight:600';
    btn.onclick = function () { location.reload(); };
    bar.appendChild(btn);
  }

  window.addEventListener('error', function (e) {
    const t = e && e.target;
    if (t && t !== window && (t.src || t.href)) showLoadError(t.src || t.href);
  }, true);

  const clamp = (v, lo, hi) => (v < lo ? lo : v > hi ? hi : v);

  const S = {
    joy: { active: false, angle: 0, magnitude: 0 },
    status: null,
    online: true,
    failCount: 0,
    rawEdit: [0, 0, 0, 0],
    rawEditing: false,
    lastRawSent: 0,
  };

  /* ---------------------------------------------------------------- 网络 */
  /* 换网/IP 变化时旧 socket 会"黑洞"，浏览器同源最多 6 个并发，卡住的请求会把
   * 后面的（包括「停车」）全堵在队列里 —— 表现就是"几十秒才反应"。
   * 所以：每个请求带超时，超时/失败就把所有在途请求一起取消，立刻腾出队列。 */
  const INFLIGHT = new Set();

  function abortInFlight() {
    INFLIGHT.forEach((ctl) => {
      try { ctl.abort(); } catch (_) { /* ignore */ }
    });
    INFLIGHT.clear();
  }

  /* 真实往返延迟统计（在手机上量的是"页面↔板子"整段，比板子侧自测更有意义） */
  const RTT = { samples: [], last: 0 };

  function recordRtt(path, ms) {
    if (path === '/api/stop' || path === '/api/estop') return;
    RTT.last = ms;
    RTT.samples.push(ms);
    while (RTT.samples.length > 12) RTT.samples.shift();
    const el = document.getElementById('chip-rtt');
    if (!el) return;
    const avg = RTT.samples.reduce((a, b) => a + b, 0) / RTT.samples.length;
    const max = Math.max.apply(null, RTT.samples);
    el.textContent = `${ms.toFixed(0)} ms  (avg ${avg.toFixed(0)} · max ${max.toFixed(0)})`;
    el.className = 'stat-v' + (avg > 150 ? ' err' : avg > 60 ? ' sim' : ' ok');
  }

  async function api(path, body, timeoutMs) {
    const ctl = new AbortController();
    const t0 = performance.now();
    INFLIGHT.add(ctl);
    const limit = timeoutMs || (body === undefined ? 2500 : 1800);
    const timer = setTimeout(() => { try { ctl.abort(); } catch (_) { /* ignore */ } }, limit);
    try {
      const opts = { method: body === undefined ? 'GET' : 'POST', headers: {}, signal: ctl.signal };
      if (body !== undefined) {
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(body);
      }
      const res = await fetch(path, opts);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return await res.json();
    } catch (err) {
      if (err && err.name === 'AbortError') {
        abortInFlight();              // 清队列：别再排队等死 socket
        throw new Error('request timeout');
      }
      throw err;
    } finally {
      clearTimeout(timer);
      INFLIGHT.delete(ctl);
      recordRtt(path, performance.now() - t0);
    }
  }

  function log(text, kind) {
    const t = new Date();
    const hh = String(t.getHours()).padStart(2, '0');
    const mm = String(t.getMinutes()).padStart(2, '0');
    const ss = String(t.getSeconds()).padStart(2, '0');
    const stamp = `${hh}:${mm}:${ss}`;

    /* 1) 常驻日志条（屏幕下方始终可见，最新的在底部并自动滚动） */
    const live = document.getElementById('live-log-body');
    if (live) {
      const line = document.createElement('div');
      if (kind) line.className = kind;
      const span = document.createElement('span');
      span.className = 't';
      span.textContent = stamp;
      line.appendChild(span);
      line.appendChild(document.createTextNode(String(text)));
      live.appendChild(line);
      while (live.children.length > 200) live.removeChild(live.firstChild);
      live.scrollTop = live.scrollHeight;
    }

    /* 2) 「More」里的完整历史（最新的在最上面） */
    const box = $('log');
    if (box) {
      const d = document.createElement('div');
      if (kind) d.className = kind;
      d.textContent = `${stamp}  ${text}`;
      box.prepend(d);
      while (box.children.length > 300) box.removeChild(box.lastChild);
    }
    if (window.console && console.log) console.log(text);
  }

  function setOnline(ok, reason) {
    if (S.online === ok) return;
    S.online = ok;
    let bar = $('offline');
    if (!ok) {
      S.joy.active = false;
      S.joy.magnitude = 0;
      $('knob').style.transform = '';
      if (!bar) {
        bar = document.createElement('div');
        bar.id = 'offline';
        bar.className = 'offline';
        document.body.appendChild(bar);
      }
      bar.textContent = 'Lost the server, commands stopped' + (reason ? ': ' + reason : '');
      log('Lost the server, commands stopped');
    } else if (bar) {
      bar.remove();
      log('Reconnected');
    }
  }

  /* ---------------------------------------------------------------- 控制量 */
  function currentControl() {
    const speedSlider = Number($('speed').value);
    const rotSlider = Number($('rot').value);
    let angle = 0;
    let velocity = 0;
    if (S.joy.active && S.joy.magnitude > 0.06) {
      angle = S.joy.angle;
      velocity = Math.round(S.joy.magnitude * (speedSlider > 0 ? speedSlider : 45));
    }
    const rot = clamp(rotSlider, -60, 60);
    if (velocity <= 0 && Math.abs(rot) < 1) {
      return { angle: 0, velocity: 0, rot: 0, drift: false };
    }
    return {
      angle: Math.round(velocity > 0 ? angle : 0) % 360,
      velocity: clamp(velocity, 0, 80),
      rot: Math.round(rot),
      drift: false,
    };
  }

  /* 注意：这里必须是「节流(throttle)」而不是「防抖(debounce)」。
   * 防抖会在连续拖动时不断重置定时器 → 只有手指停住才发请求，表现为"拖了没反应"。
   * 现在最多 20Hz 下发，且永远带最新摇杆值。 */
  let targetTimer = null;
  let lastFireAt = 0;
  const SEND_INTERVAL_MS = 50;

  function pushTarget(immediate) {
    if (!S.online) return;
    const now = performance.now();
    const fire = async () => {
      lastFireAt = performance.now();
      try {
        const json = await api('/api/target', currentControl());
        if (json && json.status) applyStatus(json.status);
      } catch (err) {
        S.failCount += 1;
        if (S.failCount > 3) setOnline(false, String(err));
      }
    };

    if (immediate || now - lastFireAt >= SEND_INTERVAL_MS) {
      if (targetTimer) { clearTimeout(targetTimer); targetTimer = null; }
      fire();
      return;
    }
    if (targetTimer) return;           // 已有排队的一次，等它发最新值即可
    targetTimer = setTimeout(() => {
      targetTimer = null;
      fire();
    }, SEND_INTERVAL_MS - (now - lastFireAt));
  }

  /* 本地即时动作文案：不等服务端返回，拖动时就更新，避免"手动了但字没动"的错觉 */
  const DIR_NAMES = [
    [0, 'Forward'], [45, 'Forward-left'], [90, 'Left'], [135, 'Back-left'],
    [180, 'Reverse'], [225, 'Back-right'], [270, 'Right'], [315, 'Forward-right'],
  ];

  function previewActionText() {
    const c = currentControl();
    if (!c.velocity && !c.rot) return 'Stopped';
    const name = (DIR_NAMES.find(([deg]) => deg === (Math.round(c.angle / 45) * 45) % 360) || [0, 'Forward'])[1];
    if (c.velocity && c.rot) {
      return `${name} + ${c.rot > 0 ? 'turn left' : 'turn right'} ${Math.abs(c.rot)}%`;
    }
    if (c.rot && !c.velocity) return `Spin ${c.rot > 0 ? 'left' : 'right'} ${Math.abs(c.rot)}%`;
    return `${name} ${c.velocity}%`;
  }

  /* ---------------------------------------------------------------- 摇杆 */
  const joy = $('joy');
  const knob = $('knob');

  function joyVector(ev) {
    const rect = joy.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const dx = (ev.clientX - cx) / (rect.width / 2);
    const dy = (cy - ev.clientY) / (rect.height / 2);
    return {
      angle: ((Math.atan2(-dx, dy) * 180) / Math.PI + 360) % 360,
      magnitude: Math.min(1, Math.hypot(dx, dy)),
    };
  }

  function moveKnob(ev) {
    const v = joyVector(ev);
    S.joy.angle = v.angle;
    S.joy.magnitude = v.magnitude;
    const rad = (v.angle * Math.PI) / 180;
    const r = v.magnitude * (joy.clientWidth / 2 - knob.clientWidth / 2 - 4);
    knob.style.transform = `translate(${-Math.sin(rad) * r}px, ${-Math.cos(rad) * r}px)`;
    const label = $('action');
    if (label) label.textContent = previewActionText();
    pushTarget(false);
  }

  joy.addEventListener('pointerdown', (ev) => {
    joy.setPointerCapture(ev.pointerId);
    S.joy.active = true;
    moveKnob(ev);
    ev.preventDefault();
  });
  joy.addEventListener('pointermove', (ev) => {
    if (S.joy.active) moveKnob(ev);
  });
  const endJoy = () => {
    if (!S.joy.active) return;
    S.joy.active = false;
    S.joy.magnitude = 0;
    knob.style.transform = '';
    const c = currentControl();
    if (c.velocity || c.rot) {
      const dirs = [[0, 'Fwd'], [45, 'F-L'], [90, 'Left'], [135, 'B-L'],
                    [180, 'Rev'], [225, 'B-R'], [270, 'Right'], [315, 'F-R']];
      const deg = (Math.round(c.angle / 45) * 45) % 360;
      const name = (dirs.find((d) => d[0] === deg) || [0, 'Fwd'])[1];
      log(`move ${name} v=${c.velocity}% rot=${c.rot}%`);
    } else {
      log('move stop');
    }
    pushTarget(true);
  };
  joy.addEventListener('pointerup', endJoy);
  joy.addEventListener('pointercancel', endJoy);
  joy.addEventListener('lostpointercapture', endJoy);

  /* ---------------------------------------------------------------- 滑块/按钮 */
  $('speed').addEventListener('change', () => log(`speed ${$('speed').value}%`));
  $('speed').addEventListener('input', () => {
    $('speed-out').textContent = $('speed').value + '%';
    pushTarget(false);
  });
  $('rot').addEventListener('input', () => {
    const v = Number($('rot').value);
    $('rot-out').textContent = (v > 0 ? '+' : '') + v + '%';
    pushTarget(false);
  });

  $('btn-stop').addEventListener('click', async () => {
    try {
      const json = await api('/api/stop', {});
      if (json && json.status) applyStatus(json.status);
      log('Stopped');
    } catch (err) {
      log('Stop failed: ' + err.message);
    }
  });

  $('btn-center').addEventListener('click', () => {
    $('speed').value = '0';
    $('rot').value = '0';
    $('speed-out').textContent = '0%';
    $('rot-out').textContent = '0%';
    S.joy.magnitude = 0;
    knob.style.transform = '';
    pushTarget(true);
  });

  /* 急停入口已从按钮行移除：长按 Stop 800ms 触发 /api/estop（误触不会触发） */
  const stopBtnEl = $('btn-stop');
  let estopTimer = null;
  let estopFired = false;
  if (stopBtnEl) {
    stopBtnEl.addEventListener('pointerdown', () => {
      estopFired = false;
      estopTimer = setTimeout(async () => {
        estopFired = true;
        try {
          await api('/api/estop', {});
          log('E-stop triggered (long press Stop)');
        } catch (err) {
          log('E-stop failed: ' + err.message);
        }
      }, 800);
    });
    ['pointerup', 'pointerleave', 'pointercancel'].forEach((ev) => {
      stopBtnEl.addEventListener(ev, () => clearTimeout(estopTimer));
    });
    stopBtnEl.addEventListener('click', (ev) => {
      if (estopFired) { ev.preventDefault(); ev.stopPropagation(); estopFired = false; }
    }, true);
  }

  /* ---------------------------------------------------------------- 单轮点检（更多里） */
  const wheelInputs = Array.from(document.querySelectorAll('input[data-wheel]'));
  wheelInputs.forEach((input) => {
    input.addEventListener('input', () => {
      const idx = Number(input.dataset.wheel);
      S.rawEdit[idx] = Number(input.value);
      input.nextElementSibling.textContent = input.value;
      S.rawEditing = true;
      clearTimeout(input._t);
      input._t = setTimeout(sendRaw, 120);
    });
  });

  async function sendRaw() {
    try {
      const json = await api('/api/raw', { wheels: S.rawEdit });
      if (json && json.status) applyStatus(json.status);
      S.lastRawSent = Date.now();
    } catch (err) {
      log('Wheel check failed: ' + err.message);
    }
  }

  const logClearBtn = $('btn-log-clear');
  if (logClearBtn) {
    logClearBtn.addEventListener('click', () => {
      const live = document.getElementById('live-log-body');
      if (live) live.innerHTML = '';
      const full = $('log');
      if (full) full.innerHTML = '';
      log('log cleared');
    });
  }

  const imuCalibBtn = $('btn-imu-calib');
  if (imuCalibBtn) {
    imuCalibBtn.addEventListener('click', async () => {
      imuCalibBtn.disabled = true;
      const old = imuCalibBtn.textContent;
      imuCalibBtn.textContent = 'calibrating...';
      try {
        const json = await api('/api/imu/calib', {}, 8000);
        const i = (json && json.status && json.status.imu) || {};
        log('IMU calib: bias ' + Number(i.bias_dps || 0).toFixed(3) + ' dps, ok=' + i.calib_ok);
      } catch (err) {
        log('IMU calib failed: ' + err.message);
      } finally {
        imuCalibBtn.disabled = false;
        imuCalibBtn.textContent = old;
      }
    });
  }

  $('btn-wheel-zero').addEventListener('click', async () => {
    S.rawEdit = [0, 0, 0, 0];
    wheelInputs.forEach((input) => {
      input.value = 0;
      input.nextElementSibling.textContent = '0';
    });
    try {
      const json = await api('/api/stop', {});
      if (json && json.status) applyStatus(json.status);
      log('All wheels zeroed');
    } catch (err) {
      log('Zero failed: ' + err.message);
    }
  });

  /* ---------------------------------------------------------------- 演示（更多里） */
  let demosLoaded = false;
  const pickerBtn = () => document.getElementById('demo-picker-btn');
  const pickerSheet = () => document.getElementById('demo-picker-sheet');
  const pickerLabel = () => document.getElementById('demo-picker-label');

  function setPickerLabel(text) {
    const el = pickerLabel();
    if (el) el.textContent = text;
  }

  function syncPickerLabel() {
    const sel = $('demo-select');
    const opt = sel && sel.options[sel.selectedIndex];
    setPickerLabel(opt ? opt.textContent : 'No demos - tap to retry');
  }

  function closePicker() {
    const sheet = pickerSheet();
    const btn = pickerBtn();
    if (sheet) sheet.hidden = true;
    if (btn) btn.setAttribute('aria-expanded', 'false');
  }

  function buildPickerItems(sequences) {
    const sheet = pickerSheet();
    if (!sheet) return;
    sheet.innerHTML = '';
    let lastGroup = null;
    sequences.forEach((s) => {
      const group = s.group || 'showcase';
      if (group !== lastGroup) {
        lastGroup = group;
        const head = document.createElement('div');
        head.className = 'demo-picker-group';
        head.textContent = group === 'check' ? 'Check / calibration' : 'Showcase';
        sheet.appendChild(head);
      }
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'demo-picker-item';
      item.dataset.name = s.name;
      item.textContent = `${s.title} · ~${s.duration}s`;
      if ($('demo-select').value === s.name) item.classList.add('selected');
      item.addEventListener('click', () => {
        const sel = $('demo-select');
        sel.value = s.name;
        sheet.querySelectorAll('.demo-picker-item').forEach((el) => el.classList.remove('selected'));
        item.classList.add('selected');
        syncPickerLabel();
        closePicker();
      });
      sheet.appendChild(item);
    });
  }

  async function loadDemos() {
    if (demosLoaded) return;
    try {
      const json = await api('/api/demo/list');
      const sel = $('demo-select');
      sel.innerHTML = '';
      (json.sequences || []).forEach((s) => {
        const opt = document.createElement('option');
        opt.value = s.name;
        opt.textContent = `${s.title} · ~${s.duration}s`;
        sel.appendChild(opt);
      });
      demosLoaded = true;
      buildPickerItems(json.sequences || []);
      syncPickerLabel();
    } catch (err) {
      setPickerLabel('No demos - tap to retry');
      log('Failed to load demo list');
    }
  }

  /* 自绘下拉：整行按钮可点，列表行高 46px，避免原生 select 箭头太小点不到 */
  document.addEventListener('click', (ev) => {
    const sheet = pickerSheet();
    const btn = pickerBtn();
    if (!sheet || !btn) return;
    if (sheet.hidden) return;
    if (!sheet.contains(ev.target) && !btn.contains(ev.target)) closePicker();
  });

  if (pickerBtn()) {
    pickerBtn().addEventListener('click', async () => {
      const sheet = pickerSheet();
      if (!sheet) return;
      if (!demosLoaded) await loadDemos();
      const willOpen = sheet.hidden;
      sheet.hidden = !willOpen;
      pickerBtn().setAttribute('aria-expanded', String(willOpen));
    });
  }

  $('btn-demo-start').addEventListener('click', async () => {
    const btn = $('btn-demo-start');
    if (btn.dataset.busy === '1') return;            // 防连点：请求在途时忽略重复点击
    btn.dataset.busy = '1';
    const label = btn.textContent;
    btn.textContent = 'Starting...';
    btn.disabled = true;
    try {
      if (!demosLoaded) await loadDemos();
      const name = $('demo-select').value;
      if (!name) return;
      const json = await api('/api/demo/start', { name }, 9000);
      if (json && json.status) applyStatus(json.status);
      log('Demo started: ' + name);
      if (window.Car3D && window.Car3D.resetPose) window.Car3D.resetPose();   // 演示开始：3D 位姿归零
    } catch (err) {
      log('Demo start failed: ' + err.message);
    } finally {
      btn.dataset.busy = '0';
      btn.disabled = false;
      btn.textContent = label;
    }
  });

  $('btn-demo-stop').addEventListener('click', async () => {
    try {
      const json = await api('/api/demo/stop', {});
      if (json && json.status) applyStatus(json.status);
      log('Demo stopped');
    } catch (err) {
      log('Demo stop failed: ' + err.message);
    }
  });

  /* ---------------------------------------------------------------- 3D */
  let car3dReady = false;

  function initStage() {
    const canvas = $('stage3d');
    car3dReady = !!(window.Car3D && window.Car3D.init(canvas));
    if (!car3dReady) {
      const box = document.querySelector('.stage');
      const tip = document.createElement('div');
      tip.className = 'fallback';
      tip.textContent = 'WebGL is not available: the 3D view is hidden, the stick and status still work.';
      box.appendChild(tip);
      $('hint').textContent = '';
      return;
    }
    window.Car3D.setActive(true);
    window.Car3D.resize();
    // 首帧布局可能还没稳定（手机地址栏/安全区），补两次 resize
    requestAnimationFrame(() => window.Car3D && window.Car3D.resize());
    setTimeout(() => window.Car3D && window.Car3D.resize(), 300);
  }

  /* ---------------------------------------------------------------- 状态渲染 */
  function setChip(id, text, cls) {
    const el = $(id);
    if (!el) return;
    el.textContent = text;
    el.className = 'stat-v' + (cls ? ' ' + cls : '');
    el.title = text;
  }

  function applyStatus(status) {
    S.status = status;
    const link = status.link || {};
    const board = status.board || {};
    const battery = status.battery || {};
    const summary = status.summary || {};

    if (link.mode === 'sim') setChip('chip-link', 'simulation (no hardware)', 'sim');
    else if (link.handshake === 'battery') setChip('chip-link', `${link.port || 'serial'} @ ${(link.baud || 1000000) / 1000 | 0}k`, 'ok');
    else setChip('chip-link', `no response on ${link.port || 'serial'}`, 'err');

    const volt = battery.voltage;
    if (volt === null || volt === undefined) setChip('chip-battery', battery.absent ? 'no battery' : '-', battery.absent ? 'err' : '');
    else setChip('chip-battery', `${Number(volt).toFixed(2)} V${battery.low ? ' (LOW)' : ''}`, battery.low ? 'err' : 'ok');

    $('action').textContent = summary.text || (status.cmd && status.cmd.mode === 'raw' ? 'Wheel check' : 'Stopped');

    /* IMU 偏航角（没装传感器时显示 off） */
    const imu = status.imu || {};
    if (imu.available && imu.heading !== null && imu.heading !== undefined) {
      const tag = imu.calib_ok ? '' : ' (calib?)';
      setChip('chip-imu', `${Number(imu.heading).toFixed(1)} deg${tag}`, imu.calib_ok ? 'ok' : 'sim');
    } else {
      setChip('chip-imu', imu.error ? 'off (' + String(imu.error).slice(0, 28) + ')' : 'off', 'sim');
    }

    /* 堵转保护状态（现场判断"车为什么停"用） */
    const sg = (status.safety && status.safety.stall_guard) || {};
    if (sg.enabled !== undefined) {
      const last = sg.last ? ` (last ${Number(sg.last.drop_mv).toFixed(0)}mV @ ${Number(sg.last.voltage).toFixed(2)}V)` : '';
      setChip('chip-stall', `${sg.trips || 0} trips${last}`, (sg.trips || 0) > 0 ? 'err' : 'ok');
    }

    /* 演示相关（注意：demo 必须先声明，否则后面引用会 TDZ 报错） */
    const demo = status.demo || {};

    /* 每次闭环转角一落定就输出到日志（不等演示结束，现场更直观） */
    const turns = demo.turns || [];
    if (S.turnLogged === undefined) S.turnLogged = 0;
    if (S.turnLogged > turns.length) S.turnLogged = 0;   /* 新一轮演示（记录被清空）时重置 */
    while (S.turnLogged < turns.length) {
      const t = turns[S.turnLogged];
      S.turnLogged += 1;
      log(`turn ${S.turnLogged}: ${t.target}° -> ${t.achieved}° (err ${t.error}°, ${t.seconds}s, ${t.reason || 'arrived'})`);
      if (t.reason && t.reason !== 'arrived') log('  ! turn finished by ' + t.reason, 'warn');
    }
    if (demo.abort_reason && S.lastAbortShown !== demo.abort_reason) {
      S.lastAbortShown = demo.abort_reason;
      log('demo aborted: ' + demo.abort_reason, 'err');
    }
    if (!demo.running && !demo.abort_reason) S.lastAbortShown = null;

    /* 顶栏进度条：演示进行中显示进度，跑完归零 */
    const bar = document.getElementById('demo-progress');
    if (bar) {
      const pct = demo.running ? Math.round((demo.progress || 0) * 100) : (demo.finished ? 100 : 0);
      bar.style.width = pct + '%';
      bar.style.opacity = demo.running ? '1' : (demo.finished ? '0.6' : '0');
    }

    if (car3dReady && window.Car3D) window.Car3D.update(status);

    if (status.safety && status.safety.limits) {
      const lim = status.safety.limits;
      const sp = $('speed');
      if (Number(sp.max) !== lim.max_velocity) sp.max = String(lim.max_velocity);
      const r = $('rot');
      if (Number(r.max) !== lim.max_rot) {
        r.min = String(-lim.max_rot);
        r.max = String(lim.max_rot);
      }
    }

    if (!S.rawEditing && status.cmd && status.cmd.mode === 'raw' && Array.isArray(status.raw)) {
      wheelInputs.forEach((input, i) => {
        const v = Number(status.raw[i] || 0);
        input.value = String(v);
        input.nextElementSibling.textContent = String(v);
        S.rawEdit[i] = v;
      });
    }
  }

  async function poll() {
    try {
      const res = await api('/api/status');
      setOnline(true);
      if (res && res.status) {
        S.failCount = 0;
        applyStatus(res.status);
      }
    } catch (err) {
      S.failCount += 1;
      if (S.failCount > 3) setOnline(false, String(err));
    }
  }

  /* ---------------------------------------------------------------- 启动 */
  function boot() {
    $('speed-out').textContent = $('speed').value + '%';
    $('rot-out').textContent = $('rot').value + '%';
    initStage();
    loadDemos();
    poll();
    setInterval(poll, 250);

    /* 说明：250ms 的 poll() 本身就是保活心跳（页面可见时浏览器不节流）。
     * 页面切到后台时浏览器会把定时器降到 1 次/分钟，这是手机侧的省电策略，网页无法绕过。 */
    window.addEventListener('resize', () => {
      if (car3dReady && window.Car3D) window.Car3D.resize();
    });
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        // 页面切走时主动停车，避免"拿着手机离开"时车还在跑
        api('/api/stop', {}).catch(() => {});
      }
    });
    log('Compact UI loaded (full UI: /index.html)');
  }

  boot();
})();
