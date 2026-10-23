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

/* 车轮控制 Demo 前端（幻尔树莓派5扩展板 A 款）
 * 数据来源：服务端 /api/status（扩展板主动上报的 SYS 帧里带电池电压）。
 * 前端只保存「期望值」并节流推送，实际下发由服务端按 0xAA55 数据包（set_motor_duty）完成。
 * 能力声明在 status.capabilities：本板逐轮独立占空，任意方向、边走边转、漂移、单轮点检都支持。
 */
(() => {
  'use strict';

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


  const $ = (id) => document.getElementById(id);
  const clamp = (v, lo, hi) => (v < lo ? lo : v > hi ? hi : v);
  const pad3 = (v) => String(Math.round(v) % 360).padStart(3, '0');

  const WHEEL_META = [
    { id: 'FL', label: 'FL' },
    { id: 'FR', label: 'FR' },
    { id: 'RR', label: 'RR' },
    { id: 'RL', label: 'RL' },
  ];

  const S = {
    online: true,
    status: null,
    lastStatus: null,
    limits: { max_velocity: 80, max_rot: 60, max_speed_mm_s: 300 },
    board: { control_mode: 'pwm', unit: '%' },
    joy: { active: false, angle: 0, magnitude: 0 },
    keys: new Set(),
    drift: false,
    wheelEdit: [0, 0, 0, 0],
    wheelEditing: false,
    demoStepsFor: null,
    demoSequences: [],
    syncedFromServer: false,
    telemetryStaleLogged: false,
    linkErrorLogged: false,
    batteryLowLogged: false,
    batteryAbsentLogged: false,
    flashMismatchLogged: false,
    invertLoged: false,
    failCount: 0,
  };

  /* ------------------------------------------------------------------ 网络 */
  const INFLIGHT = new Set();

  function abortInFlight() {
    INFLIGHT.forEach(function (ctl) {
      try { ctl.abort(); } catch (_) { /* ignore */ }
    });
    INFLIGHT.clear();
  }

  async function api(path, body, timeoutMs) {
    const ctl = new AbortController();
    INFLIGHT.add(ctl);
    const limit = timeoutMs || (body === undefined ? 2500 : 1800);
    const timer = setTimeout(function () { try { ctl.abort(); } catch (_) { /* ignore */ } }, limit);
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
        abortInFlight();
        throw new Error('request timeout');
      }
      throw err;
    } finally {
      clearTimeout(timer);
      INFLIGHT.delete(ctl);
    }
  }

  /* 红条：点一下重新加载（旧版本卡住的页面能自己恢复） */
  function wireOfflineBar() {
    const bar = $('offline-bar');
    if (!bar || bar.dataset.wired) return;
    bar.dataset.wired = '1';
    bar.style.cursor = 'pointer';
    bar.title = 'Click to reload the page';
    bar.addEventListener('click', () => location.reload());
  }

  /* 看门狗：可见状态下 10s 没有成功请求就自动重载一次（30s 内不重复），
   * 防止"红条卡死/轮询循环挂掉"后再也不恢复。 */
  function startWatchdog() {
    setInterval(() => {
      if (document.hidden) return;
      if (!S.online || !S.lastOkAt) return;
      if (Date.now() - S.lastOkAt < 10000) return;
      let last = 0;
      try { last = Number(sessionStorage.getItem('wc-last-reload') || 0); } catch (_) { last = 0; }
      if (Date.now() - last < 30000) return;
      try { sessionStorage.setItem('wc-last-reload', String(Date.now())); } catch (_) { /* ignore */ }
      location.reload();
    }, 3000);
  }

  function setOnline(ok, reason) {
    if (S.online === ok) return;
    S.online = ok;
    const bar = $('offline-bar');
    if (bar) bar.hidden = ok;
    if (!ok) {
      S.joy.active = false;
      S.joy.magnitude = 0;
      $('joy-knob').style.transform = '';
      pushLog('Lost the server, commands stopped', 'err');
    } else {
      pushLog('Reconnected' + (reason ? ': ' + reason : ''), 'ok');
    }
  }

  /* ------------------------------------------------------------------ 控制量 */
  function keyboardVector() {
    const k = S.keys;
    let dx = 0;
    let dy = 0;
    if (k.has('w') || k.has('arrowup')) dy += 1;
    if (k.has('s') || k.has('arrowdown')) dy -= 1;
    if (k.has('a') || k.has('arrowleft')) dx -= 1;
    if (k.has('d') || k.has('arrowright')) dx += 1;
    return { dx, dy };
  }

  function computeControl() {
    const vSlider = Number($('velocity').value);
    const rotSlider = Number($('rot').value);

    let angle = 0;
    let velocity = 0;

    if (S.joy.active && S.joy.magnitude > 0.05) {
      angle = S.joy.angle;
      velocity = Math.round(S.joy.magnitude * (vSlider > 0 ? vSlider : 45));
    } else {
      const { dx, dy } = keyboardVector();
      if (dx !== 0 || dy !== 0) {
        angle = (Math.atan2(-dx, dy) * 180) / Math.PI;
        angle = (angle + 360) % 360;
        velocity = vSlider > 0 ? vSlider : 45;
      }
    }

    let rot = rotSlider;
    if (S.keys.has('q')) rot += 25;
    if (S.keys.has('e')) rot -= 25;
    rot = clamp(rot, -S.limits.max_rot, S.limits.max_rot);

    return {
      angle: Math.round(velocity > 0 ? angle : 0) % 360,
      velocity: clamp(Math.round(velocity), 0, S.limits.max_velocity),
      rot: Math.round(rot),
      drift: S.drift,
    };
  }

  /* 摇杆每帧都会调用 pushTarget：这里做「节流」，最多 20Hz，永远发最新值。
   * 之前是每个 pointermove 直接发一次（可达 60 次/秒），弱网下会把队列压满、反而更卡。 */
  let lastFireAt = 0;
  let pendingTimer = null;
  let pendingDirty = false;
  const SEND_INTERVAL_MS = 50;

  async function sendTarget() {
    const json = await api('/api/target', computeControl()).catch(() => null);
    if (json && json.status) {
      S.status = json.status;
      render();
    }
  }

  function pushTarget() {
    if (!S.online) return;
    const now = performance.now();
    if (now - lastFireAt >= SEND_INTERVAL_MS) {
      lastFireAt = now;
      pendingDirty = false;
      sendTarget();
      return;
    }
    pendingDirty = true;
    if (pendingTimer) return;
    pendingTimer = setTimeout(() => {
      pendingTimer = null;
      if (!pendingDirty) return;
      pendingDirty = false;
      lastFireAt = performance.now();
      sendTarget();
    }, SEND_INTERVAL_MS - (now - lastFireAt));
  }

  /* ------------------------------------------------------------------ 摇杆 */
  const joy = $('joy');
  const knob = $('joy-knob');

  function joyFromEvent(ev) {
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

  function setKnob(x, y) {
    knob.style.transform = `translate(${x}px, ${y}px)`;
  }

  function applyJoy(ev) {
    const p = joyFromEvent(ev);
    S.joy.angle = p.angle;
    S.joy.magnitude = p.magnitude;
    const rad = (p.angle * Math.PI) / 180;
    const r = p.magnitude * 68;
    setKnob(-Math.sin(rad) * r, -Math.cos(rad) * r);
    pushTarget();
  }

  joy.addEventListener('pointerdown', (ev) => {
    joy.setPointerCapture(ev.pointerId);
    joy.classList.add('dragging');
    S.joy.active = true;
    applyJoy(ev);
    ev.preventDefault();
  });

  joy.addEventListener('pointermove', (ev) => {
    if (!S.joy.active) return;
    applyJoy(ev);
  });

  const endJoy = () => {
    if (!S.joy.active) return;
    S.joy.active = false;
    S.joy.magnitude = 0;
    joy.classList.remove('dragging');
    setKnob(0, 0);
    pushTarget();
  };

  joy.addEventListener('pointerup', endJoy);
  joy.addEventListener('pointercancel', endJoy);
  joy.addEventListener('lostpointercapture', endJoy);

  /* ------------------------------------------------------------------ 滑块 */
  $('velocity').addEventListener('input', () => {
    $('velocity-out').textContent = $('velocity').value + '%';
    pushTarget();
  });

  $('rot').addEventListener('input', () => {
    const v = Number($('rot').value);
    $('rot-out').textContent = (v > 0 ? '+' : '') + v + '%';
    pushTarget();
  });

  $('drift').addEventListener('change', () => {
    S.drift = $('drift').checked;
    pushLog(S.drift ? 'Drift mode on' : 'Drift mode off', S.drift ? 'warn' : '');
    pushTarget();
  });

  function resetLocalInputs() {
    S.joy.active = false;
    S.joy.magnitude = 0;
    setKnob(0, 0);
    $('velocity').value = 0;
    $('velocity-out').textContent = '0%';
    $('rot').value = 0;
    $('rot-out').textContent = '0%';
  }

  $('btn-stop').addEventListener('click', async () => {
    resetLocalInputs();
    const res = await api('/api/stop', {}).catch(() => null);
    if (res && res.status) S.status = res.status;
    pushLog('Stopped (duties zeroed)', '');
    render();
  });

  $('btn-reset-aim').addEventListener('click', () => {
    S.joy.active = false;
    S.joy.magnitude = 0;
    S.keys.clear();
    setKnob(0, 0);
    $('velocity').value = 45;
    $('velocity-out').textContent = '45%';
    $('rot').value = 0;
    $('rot-out').textContent = '0%';
    pushTarget();
  });

  const estopBtn = $('btn-estop');   /* 顶栏已去掉该按钮：存在才绑定 */
  if (estopBtn) estopBtn.addEventListener('click', async () => {
    resetLocalInputs();
    S.keys.clear();
    const res = await api('/api/estop', {}).catch(() => null);
    if (res && res.status) S.status = res.status;
    pushLog('E-stop: duties zeroed, demo cancelled', 'err');
    render();
  });

  /* ------------------------------------------------------------------ 键盘 */
  const KEY_WHITELIST = new Set([
    'w', 'a', 's', 'd', 'q', 'e',
    'arrowup', 'arrowdown', 'arrowleft', 'arrowright',
  ]);

  const isTyping = (el) => el instanceof HTMLInputElement || el instanceof HTMLSelectElement;

  window.addEventListener('keydown', (ev) => {
    const key = ev.key.toLowerCase();
    /* 顶栏 E-STOP 按钮已去掉：Shift+Esc 作为急停快捷键（Space/Esc 仍是普通停车） */
    if (key === 'escape' && ev.shiftKey && !isTyping(ev.target)) {
      ev.preventDefault();
      api('/api/estop', {}).catch(() => null);
      pushLog('E-stop triggered (Shift+Esc)', 'err');
      return;
    }
    if (key === ' ' || key === 'escape') {
      if (isTyping(ev.target)) return;
      ev.preventDefault();
      $('btn-stop').click();
      return;
    }
    if (!KEY_WHITELIST.has(key) || isTyping(ev.target)) return;
    ev.preventDefault();
    if (S.keys.has(key)) return;
    S.keys.add(key);
    pushTarget();
  });

  window.addEventListener('keyup', (ev) => {
    const key = ev.key.toLowerCase();
    if (!S.keys.has(key)) return;
    S.keys.delete(key);
    if (KEY_WHITELIST.has(key)) pushTarget();
  });

  window.addEventListener('blur', () => {
    if (S.keys.size) {
      S.keys.clear();
      pushTarget();
    }
    endJoy();
  });

  /* ------------------------------------------------------------------ 单轮点检 */
  function perWheelSupported() {
    const caps = (S.status && S.status.capabilities) || {};
    return caps.per_wheel !== false;
  }

  function buildWheelEditor() {
    const box = $('wheel-editor');
    box.innerHTML = '';
    WHEEL_META.forEach((meta, idx) => {
      const row = document.createElement('div');
      row.className = 'wheel-edit';
      row.innerHTML = `
        <span class="name">${meta.id}</span>
        <input type="range" min="-100" max="100" step="5" value="0" data-wheel="${idx}">
        <output>0</output>`;
      const input = row.querySelector('input');
      /* 本板支持逐轮独立占空：滑块直接写该轮的占空（编号 M1~M4） */
      input.disabled = !perWheelSupported();
      input.title = input.disabled ? 'per-wheel control unavailable' : 'wheel duty (-100..100)';
      const out = row.querySelector('output');
      let timer = null;
      input.addEventListener('input', () => {
        out.textContent = input.value;
        S.wheelEdit[idx] = Number(input.value);
        S.wheelEditing = true;
        clearTimeout(timer);
        timer = setTimeout(sendRaw, 90);
      });
      box.appendChild(row);
    });
  }

  async function sendRaw() {
    if (!perWheelSupported()) {
      pushLog('Per-wheel control unsupported: wheel check disabled', 'warn');
      return;
    }
    const res = await api('/api/raw', { wheels: S.wheelEdit }).catch(() => null);
    if (res && res.status) {
      S.status = res.status;
      render();
    }
  }

  $('btn-wheel-zero').addEventListener('click', () => {
    S.wheelEdit = [0, 0, 0, 0];
    document.querySelectorAll('#wheel-editor input').forEach((input) => {
      input.value = 0;
      input.nextElementSibling.textContent = '0';
    });
    S.wheelEditing = false;
    api('/api/stop', {}).then((res) => {
      if (res && res.status) {
        S.status = res.status;
        render();
      }
    }).catch(() => null);
    pushLog('All wheels zeroed', '');
  });

  /* ------------------------------------------------------------------ 演示 */
  async function loadDemoList() {
    const res = await api('/api/demo/list').catch(() => null);
    if (!res || !res.sequences) return;
    S.demoSequences = res.sequences;
    const select = $('demo-select');
    select.innerHTML = '';
    res.sequences.forEach((seq) => {
      const opt = document.createElement('option');
      opt.value = seq.name;
      opt.textContent = `${seq.group === 'check' ? '[check] ' : ''}${seq.title} · ~${seq.duration}s`;
      opt.title = seq.description;
      select.appendChild(opt);
    });
    showSelectedDescription();
  }

  function showSelectedDescription() {
    const seqs = S.demoSequences || [];
    const select = $('demo-select');
    if (!select) return;
    const seq = seqs.find((item) => item.name === select.value);
    const demo = S.status && S.status.demo;
    const caption = $('demo-caption');      /* 桌面版已移除该元素，允许为 null */
    if (caption && seq && !(demo && (demo.running || demo.finished))) {
      caption.textContent = seq.description;
    }
    if (select && seq) select.title = seq.description;   /* 提示改挂到下拉框上 */
  }

  $('demo-select').addEventListener('change', showSelectedDescription);

  $('btn-demo-start').addEventListener('click', async () => {
    const name = $('demo-select').value;
    if (!name) return;
    const res = await api('/api/demo/start', { name }, 9000).catch(() => null);
    if (res && res.status) {
      S.status = res.status;
      pushLog(`Demo started: ${$('demo-select').selectedOptions[0].textContent}`, 'warn');
      if (window.Car3D && window.Car3D.resetPose) window.Car3D.resetPose();   // 演示开始：3D 位姿归零
      render();
    }
  });

  const pauseBtn0 = $('btn-demo-pause');   /* 顶栏已去掉 Pause：存在才绑定 */
  if (pauseBtn0) pauseBtn0.addEventListener('click', async () => {
    const demo = S.status && S.status.demo;
    const path = demo && demo.paused ? '/api/demo/resume' : '/api/demo/pause';
    const res = await api(path, {}).catch(() => null);
    if (res && res.status) {
      S.status = res.status;
      pushLog(demo && demo.paused ? 'Demo resumed' : 'Demo paused', '');
      render();
    }
  });

  $('btn-demo-stop').addEventListener('click', async () => {
    const res = await api('/api/demo/stop', {}).catch(() => null);
    if (res && res.status) {
      S.status = res.status;
      pushLog('Demo stopped', '');
      render();
    }
  });

  /* ------------------------------------------------------------------ 日志 */
  function pushLog(text, level) {
    const list = $('log');
    const li = document.createElement('li');
    if (level) li.className = level;
    const now = new Date();
    const time = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}:${String(now.getSeconds()).padStart(2, '0')}`;
    li.innerHTML = `<time>${time}</time><span></span>`;
    li.querySelector('span').textContent = text;
    list.prepend(li);
    while (list.children.length > 30) list.removeChild(list.lastChild);
  }

  /* ------------------------------------------------------------------ 渲染 */
  let readoutCells = [];

  function buildReadouts() {
    const box = $('wheel-readout');
    box.innerHTML = '';
    readoutCells = WHEEL_META.map((meta) => {
      const cell = document.createElement('div');
      cell.className = 'wr-cell';
      cell.innerHTML = `
        <div class="wr-head"><span class="wr-name">${meta.id} ${meta.label}</span><span class="wr-dir">-</span></div>
        <div class="wr-value">0%</div>
        <div class="wr-bar"><span style="left:50%;width:0"></span></div>
        <div class="wr-meas">measured -</div>
        <div class="wr-meta">cmd - · encoder -</div>`;
      box.appendChild(cell);
      return {
        name: cell.querySelector('.wr-name'),
        dir: cell.querySelector('.wr-dir'),
        value: cell.querySelector('.wr-value'),
        bar: cell.querySelector('.wr-bar span'),
        meas: cell.querySelector('.wr-meas'),
        meta: cell.querySelector('.wr-meta'),
      };
    });
  }

  function chipText(id, text, cls) {
    const el = $(id);
    if (!el) return;          /* 状态栏已移除：元素不存在就静默跳过 */
    el.textContent = text;
    el.className = 'chip ' + cls;
  }

  function chipTitle(id, text) {
    const el = $(id);
    if (!el) return;
    el.title = text || '';
  }

  function renderChips(status) {
    const link = status.link || {};
    const board = status.board || {};
    const telemetry = status.telemetry || {};
    const cmd = status.cmd || {};
    const sim = link.mode === 'sim';

    if (sim) chipText('chip-link', 'Link: simulation (no hardware)', 'chip-sim');
    else if (link.connected) chipText('chip-link', `Link: ${link.port || 'serial'}`, 'chip-ok');
    else chipText('chip-link', 'Link: disconnected', 'chip-err');

    /* 降级到仿真的原因（例如自动探测失败、缺 pyserial）挂在链路标签上，并只提示一次 */
    chipTitle('chip-link', link.error || '');
    if (link.error && !S.linkErrorLogged) {
      S.linkErrorLogged = true;
      pushLog('Link warning: ' + link.error, 'warn');
    }

    const modeText = 'RasAdapter5 A · 4-ch duty (1 Mbps)';
    chipText('chip-mode', `Mode: ${modeText}`, 'chip-muted');

    const hs = link.handshake || '-';
    const hsMap = {
      battery: ['SYS battery frame OK', 'chip-ok'],
      reply: ['reply ok (no voltage)', 'chip-ok'],
      sim: ['simulated board', 'chip-ok'],
      'no-response': ['no response', 'chip-err'],
      failed: ['handshake failed', 'chip-err'],
      pending: ['pending', 'chip-muted'],
    };
    const [hsText, hsCls] = hsMap[hs] || [hs, 'chip-muted'];
    chipText('chip-handshake', `Handshake: ${hsText}`, hsCls);

    chipText('chip-hz', `Poll: ${(cmd.hz || 0).toFixed(0)} Hz · ${cmd.sent || 0} frames sent`,
      cmd.errors ? 'chip-err' : 'chip-muted');

    const age = telemetry.age_s;
    const stale = telemetry.stale;
    const openLoop = board.open_loop;
    const hasEncoder = board.has_encoder;
    if (telemetry.link_ok) {
      chipText('chip-telemetry',
        `Telemetry: ${Number(age).toFixed(1)}s ago · ${telemetry.frames || 0} frames (no encoder)`, 'chip-ok');
    } else if (sim) {
      chipText('chip-telemetry', 'Telemetry: simulation', 'chip-sim');
    } else {
      chipText('chip-telemetry', 'Telemetry: no packets (check port / 1 Mbps)', 'chip-err');
    }

    if (sim) {
      $('stage-hint').textContent = 'Simulated: per-wheel duty, ~9 Hz battery reports, no encoder';
    } else {
      $('stage-hint').textContent = 'Live: wheels follow the real duties (no encoder, mm/s estimated)';
    }

    if (telemetry.stale && !sim && !openLoop) {
      if (!S.telemetryStaleLogged) {
        pushLog('Telemetry interrupted; showing commanded values only', 'warn');
        S.telemetryStaleLogged = true;
      }
    } else {
      S.telemetryStaleLogged = false;
    }

    const battery = status.battery || {};
    if (battery.low && !S.batteryLowLogged) {
      pushLog(`Battery ${Number(battery.voltage).toFixed(2)}V is low (threshold ${battery.low_v}V) - please charge`, 'warn');
      S.batteryLowLogged = true;
    } else if (!battery.low) {
      S.batteryLowLogged = false;
    }
    if (battery.absent && !S.batteryAbsentLogged) {
      pushLog('0V reading: no battery (or bad sample) - motors will not move', 'warn');
      S.batteryAbsentLogged = true;
    } else if (!battery.absent) {
      S.batteryAbsentLogged = false;
    }

    const estopEl = $('btn-estop');
    if (estopEl) estopEl.textContent = (status.safety && status.safety.estop) ? 'E-STOPPED' : 'E-STOP';
  }

  function renderFeedback(status) {
    const telemetry = status.telemetry || {};
    const board = status.board || {};
    const battery = status.battery || {};

    const volt = battery.voltage;
    const batteryEl = $('fb-battery');
    if (volt === null || volt === undefined) {
      batteryEl.textContent = '-';
      batteryEl.style.color = '';
      $('fb-battery-hint').textContent = 'pushed by the board (SYS)';
    } else if (battery.absent) {
      batteryEl.textContent = `${Number(volt).toFixed(2)} V`;
      batteryEl.style.color = '';
      $('fb-battery-hint').textContent = 'no battery (or bad sample)';
    } else {
      batteryEl.textContent = `${Number(volt).toFixed(2)} V`;
      batteryEl.style.color = battery.low ? '#e5484d' : '';
      $('fb-battery-hint').textContent = battery.low
        ? `low (< ${battery.low_v} V) - charge`
        : `full ~ ${battery.full_v} V`;
    }

    const ageEl = $('fb-age');
    if (telemetry.age_s === null || telemetry.age_s === undefined) {
      ageEl.textContent = '-';
      ageEl.style.color = '';
      $('fb-age-hint').textContent = 'no report yet';
    } else {
      ageEl.textContent = `${Number(telemetry.age_s).toFixed(2)} s`;
      ageEl.style.color = telemetry.link_ok ? '' : '#e5484d';
      $('fb-age-hint').textContent = 'flagged if no SYS frame for 2 s';
    }

    $('fb-frames').textContent = `${telemetry.frames || 0} / ${telemetry.acks || 0}`;
    $('fb-board').textContent = `${board.motor_label || '-'}`;
    $('fb-proto').textContent =
      `${board.protocol || '1 Mbps 8N1'} · deadzone ${Number(board.deadzone_pct || 0).toFixed(0)}%`;

    const invert = board.invert || {};
    if (invert.motors && !S.invertLoged) {
      pushLog('invert_motors active: all 4 duties are sign-flipped so "forward" = nose direction (calibration, not a fault)', '');
      S.invertLoged = true;
    }

    if (board.flash_mismatch && !S.flashMismatchLogged) {
      pushLog(board.flash_mismatch, 'warn');
      S.flashMismatchLogged = true;
    }

    const unknown = telemetry.unknown || [];
    const box = $('fb-unknown');
    if (unknown.length) {
      box.hidden = false;
      box.textContent = 'Unknown frames: ' + unknown.join('  ');
    } else {
      box.hidden = true;
    }
  }

  function renderReadouts(status) {
    const wheels = status.wheels || [];
    const unit = (status.board && status.board.unit) || 'mm/s';
    wheels.forEach((w, i) => {
      const cell = readoutCells[i];
      if (!cell) return;
      const v = Number(w.value) || 0;
      cell.name.textContent = `${w.channel || ''} ${w.label || ''}`.trim();
      cell.value.textContent = (v > 0 ? '+' : '') + v + '%';
      cell.dir.textContent = v === 0 ? 'idle' : v > 0 ? 'fwd' : 'rev';
      const pct = Math.min(100, Math.abs(v));
      cell.bar.style.width = pct / 2 + '%';
      cell.bar.style.left = v >= 0 ? '50%' : 50 - pct / 2 + '%';

      const measured = Number(w.measured_mm_s) || 0;
      const fromEncoder = w.measured_source === 'encoder';
      cell.meas.textContent = `${fromEncoder ? 'measured' : 'est.'} ${measured} mm/s`;
      cell.meas.style.color = measured === 0 ? '#8a95a3' : (fromEncoder ? '#159d63' : '#c9770a');

      const command = Number(w.command) || 0;
      cell.meta.textContent = `sent ${command}% · no encoder`;
    });
  }

  function renderStage(status) {
    const summary = status.summary || {};
    $('command-text').textContent = status.cmd.line || '-';
    $('action-text').textContent = summary.text || 'Stopped';

    /* 方向角显示服务端实际执行的方向（本板逐轮独立占空，不需要吸附） */
    const want = status.target || status.applied || {};
    $('angle-text').textContent = pad3(want.angle || 0) + '°';   // 显示用户意图的角度（不发镜像值）

    $('wheel-mode-hint').textContent = status.capabilities && status.capabilities.per_wheel === false
      ? 'This board: whole-car motion only'
      : 'Vector mode (stick / keyboard)';
  }

  function renderDemo(status) {
    const demo = status.demo || {};
    const bar = $('demo-progress');
    const caption = $('demo-caption');       /* 桌面版已移除，允许为 null */
    const time = $('demo-time');
    const pauseBtn = $('btn-demo-pause');

    if (bar) bar.style.width = Math.round((demo.progress || 0) * 100) + '%';
    const done = (demo.duration || 0) * (demo.progress || 0);
    if (time) {
      if (demo.finished && demo.last_run_s) {
        /* 跑完：给出"上次实际耗时 vs 脚本时长"，一眼看出准不准 */
        const diff = demo.last_run_s - (demo.duration || 0);
        time.textContent = `actual ${demo.last_run_s.toFixed(2)}s / scripted ${(demo.duration || 0).toFixed(2)}s`
          + ` (${diff >= 0 ? '+' : ''}${diff.toFixed(2)}s)`;
      } else {
        time.textContent = `${done.toFixed(1)}s / ${(demo.duration || 0).toFixed(1)}s (scripted)`;
      }
    }

    if (caption) {
      if (demo.running) {
        caption.textContent = (demo.paused ? '⏸ ' : '▶ ') + (demo.caption || '') +
          ` (step ${demo.index + 1}/${demo.step_count})`;
      } else if (demo.finished) {
        caption.textContent = `✔ ${demo.title || 'Demo'} finished`;
      } else {
        const seq = (S.demoSequences || []).find((item) => item.name === $('demo-select').value);
        caption.textContent = seq ? seq.description : 'Pick a sequence, then click Start demo';
      }
    }

    if (pauseBtn) {
      pauseBtn.textContent = demo.paused ? 'Resume' : 'Pause';
      pauseBtn.disabled = !demo.running;
    }
    $('btn-demo-start').disabled = Boolean(demo.running);
    $('btn-demo-stop').disabled = !demo.running;

    if (S.demoStepsFor !== demo.name) {
      S.demoStepsFor = demo.name;
      const box = $('demo-steps');          /* 演示步骤列表已从桌面版移除，允许为 null */
      if (box) {
        box.innerHTML = '';
        (demo.steps || []).forEach((step) => {
          const li = document.createElement('li');
          li.textContent = step.caption;
          li.title = `${step.mode === 'raw' ? 'wheel ' + JSON.stringify(step.raw) : `${step.angle}deg ${step.velocity}% spin${step.rot}%`} · ${step.duration}s`;
          box.appendChild(li);
        });
      }
    }

    const stepsBox = $('demo-steps');
    if (stepsBox) {
      const items = stepsBox.children;
      for (let i = 0; i < items.length; i += 1) {
        const isCurrent = demo.running && i === demo.index;
        const isDone = demo.running || demo.finished ? i < demo.index || demo.finished : false;
        items[i].classList.toggle('current', isCurrent);
        items[i].classList.toggle('done', isDone && !isCurrent);
      }
    }
  }

  function render() {
    const status = S.status;
    if (!status) return;
    if (window.Car3D && window.Car3D.ready) window.Car3D.update(status);
    renderChips(status);
    renderReadouts(status);
    renderStage(status);
    renderFeedback(status);
    renderDemo(status);

    /* 若板型不支持单轮直控，才把点检滑块锁成只读展示 */
    if (!perWheelSupported()) {
      const caps = status.capabilities || {};
      document.querySelectorAll('#wheel-editor input').forEach((input) => {
        if (!input.disabled) {
          input.disabled = true;
          input.title = 'per-wheel control unavailable';
        }
      });
      if (caps.angle_step_deg) {
        $('wheel-mode-hint').title = `heading step is ${caps.angle_step_deg} deg`;
      }
    }

    if (!S.wheelEditing && status.cmd.mode === 'raw') {
      const raw = status.raw || [];
      document.querySelectorAll('#wheel-editor input').forEach((input, i) => {
        input.value = raw[i] || 0;
        input.nextElementSibling.textContent = raw[i] || 0;
        S.wheelEdit[i] = raw[i] || 0;
      });
    }

    const limits = status.safety && status.safety.limits;
    if (limits) {
      S.limits = limits;
      const vEl = $('velocity');
      if (Number(vEl.max) !== limits.max_velocity) vEl.max = String(limits.max_velocity);
      const rEl = $('rot');
      if (Number(rEl.max) !== limits.max_rot) {
        rEl.min = String(-limits.max_rot);
        rEl.max = String(limits.max_rot);
      }
    }

    $('footer-time').textContent = 'Uptime ' + (status.uptime_s || 0).toFixed(0) + 's';
  }

  /* ------------------------------------------------------------------ 舞台（仅 3D） */
  /* 本版本只保留 3D 演示；2D 平面图已移除。
   * WebGL 不可用时在舞台上给一条可见提示，不做 2D 回退（避免"看起来能用"的假象）。 */
  function showStageFallback(message) {
    const stage = document.querySelector('.stage');
    if (!stage) {
      pushLog(message, 'warn');
      return;
    }
    let el = document.getElementById('stage-fallback');
    if (!el) {
      el = document.createElement('div');
      el.id = 'stage-fallback';
      el.className = 'stage-fallback';
      stage.appendChild(el);
    }
    el.textContent = message;
    el.hidden = false;
  }

  /* ------------------------------------------------------------------ 轮询 */
  async function pollStatus() {
    try {
      const res = await api('/api/status');
      S.failCount = 0;
      S.lastOkAt = Date.now();
      setOnline(true);
      if (res && res.status) {
        const prev = S.lastStatus;
        S.lastStatus = res.status;
        S.status = res.status;
        S.limits = (res.status.safety && res.status.safety.limits) || S.limits;

        if (!S.syncedFromServer) {
          S.syncedFromServer = true;
          const t = res.status.target || {};
          const demoRunning = res.status.demo && res.status.demo.running;
          if (!demoRunning && res.status.cmd.mode === 'vector') {
            $('velocity').value = String(t.velocity || 0);
            $('velocity-out').textContent = (t.velocity || 0) + '%';
            $('rot').value = String(t.rot || 0);
            $('rot-out').textContent = ((t.rot || 0) > 0 ? '+' : '') + (t.rot || 0) + '%';
            S.drift = Boolean(t.drift);
            $('drift').checked = S.drift;
          }
        }

        if (prev && res.status.demo && prev.demo && res.status.demo.finished && !prev.demo.finished) {
          pushLog(`Demo "${res.status.demo.title}" finished`, 'ok');
        }
        if (prev && res.status.link.error && res.status.link.error !== prev.link.error) {
          pushLog('Link error: ' + res.status.link.error, 'err');
        }
        render();
      }
    } catch (err) {
      S.failCount += 1;
      if (S.failCount > 3) setOnline(false, String(err));
    }
  }

  wireOfflineBar();
  startWatchdog();

  /* ------------------------------------------------------------------ 启动 */
  function initStage() {
    const stage3d = document.getElementById('stage3d');
    const ok = !!(window.Car3D && window.Car3D.init(stage3d));
    if (!ok) {
      showStageFallback('WebGL is not available, the 3D stage cannot be shown; live values are still in the wheel cards and Board feedback.');
      pushLog('WebGL unavailable: 3D stage skipped', 'warn');
      return;
    }
    if (stage3d) stage3d.hidden = false;
    window.Car3D.setActive(true);
    window.Car3D.resize();
  }

  function init() {
    buildWheelEditor();
    buildReadouts();
    loadDemoList();
    initStage();
    window.addEventListener('resize', function () {
      if (window.Car3D && window.Car3D.ready) window.Car3D.resize();
    });
    pollStatus();
    setInterval(pollStatus, 200);
    pushLog('Page loaded, waiting for server', '');
  }

  init();
})();
