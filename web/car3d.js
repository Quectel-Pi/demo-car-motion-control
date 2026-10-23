/* 小车 3D 演示（WebGL / three.js）
 *
 * 设计要点：
 *   - 只依赖本地 vendored 的 three.min.js（离线可用，无 CDN）；
 *   - 车轮纹理用 canvas 画 45° 斜纹模拟麦克纳姆轮滚子，转速由指令轮速驱动；
 *   - 车体保持不动，用地面网格反向平移 + 矢量箭头 + 旋转弧线表达运动；
 *   - 若 WebGL 不可用或 three 未加载，返回 null，页面自动回退到 2D 平面图。
 */
(function () {
  'use strict';

  /* 轮位（世界坐标，右手系：+y 上、+z 车头）→ 车的左侧是 +x。
   *   按现场接线：M1=左前 / M2=左后 / M3=右前 / M4=右后，所以左前在 x=+1。 */
  const WHEEL_META = [
    { id: 'FL', label: 'FL', pos: [1, 1] },
    { id: 'FR', label: 'FR', pos: [-1, 1] },
    { id: 'RR', label: 'RR', pos: [-1, -1] },
    { id: 'RL', label: 'RL', pos: [1, -1] },
  ];

  /* 舞台配色：默认浅色（/ 与 /index.html）；深色页（/dark）在加载本文件前把
   * window.WHEEL_STAGE_THEME 置为 'dark'，即可切到深色科技风而且不影响亮色页。 */
  const STAGE_DARK = typeof window !== 'undefined' && window.WHEEL_STAGE_THEME === 'dark';
  const THEME = STAGE_DARK
    ? {
      bg: 0x070d19,
      gridMajor: 0x2f5b8c,
      gridMinor: 0x182c45,
      gridOpacity: 0.75,
      ground: 0x0b1523,
      hemiSky: 0xe4edff,
      hemiGround: 0x101f33,
      hemiIntensity: 1.0,
      dirIntensity: 1.15,
      idleWheel: 0x8f8f8f,
    }
    : {
      bg: 0xf4f8ff,
      gridMajor: 0x9fb6d8,
      gridMinor: 0xcbd9ee,
      gridOpacity: 1,
      ground: 0xeef3fb,
      hemiSky: 0xffffff,
      hemiGround: 0xd8e2f0,
      hemiIntensity: 0.95,
      dirIntensity: 0.85,
      idleWheel: 0xd9e0ea,
    };

  const state = {
    ready: false,
    scene: null, camera: null, renderer: null, canvas: null,
    carGroup: null,
    wheels: [],
    grid: null,
    arrow: null,
    arrowMat: null,
    rotRing: null,
    spin: [0, 0, 0, 0],
    tilt: { x: 0, z: 0 },
    target: { vx: 0, vy: 0, omega: 0 },
    phase: { vx: 0, vy: 0, omega: 0 },
    orbit: { yaw: 0.88, pitch: 0.50, dist: 3.55, dragging: false, lx: 0, ly: 0 },
    pose: { x: 0, z: 0, yaw: 0 },        // 车在场景里的估算位姿（只用于运动跟随，不显示距离）
    cameraMode: 'fixed',                 // 'fixed' = 地面不动、车移动；'follow' = 相机追车
    imuHeading: null,                    // 真机 IMU 朝向（°/可用时优先用它当航向）
    // 航向显示符号：+1 = 模型约定（+x 为车左侧，rotation.y=+yaw 时车头从 +z 转向 +x = 逆时针）。
    // 现场已用"手动左转 + 观察车头"确认 +1 正确；异常时可在控制台 Car3D.setYawSign(-1) 临时镜像。
    yawSign: 1,
    camTarget: null,                     // 相机注视点（平滑跟随车）
    lastTs: 0,
    active: false,
  };

  /* 车体/轮子配色：按实车特写（黄色星形轮盘 + 黑色橡胶辊子 + 深灰辐条轮毂） */
  const SKIN = STAGE_DARK
    ? {
      roller: 0x17191d,
      plate: 0xf3c300,
      body: 0x242a33,
      top: 0x2d3540,
      pcb: 0x1c4a30,
      accent: 0x22d3ee,
      hub: 0x2b3038,
      hubMetal: 0.55,
      metal: 0x9aa3ad,
      labelBg: 'rgba(7,13,25,0.9)',
      labelText: '#eaf3ff',
      labelBorder: '#22d3ee',
    }
    : {
      roller: 0x1b1d21,
      plate: 0xf3c300,
      body: 0xf7fafe,
      top: 0xffffff,
      pcb: 0x2f6fed,
      accent: 0x2f6fed,
      hub: 0x33383f,
      hubMetal: 0.5,
      metal: 0x9aa3ad,
      labelBg: 'rgba(255,255,255,0.92)',
      labelText: '#1b2430',
      labelBorder: '#f0932b',
    };

  /* 轮位 → 扩展板电机编号（接线事实：FL=M1 / FR=M3 / RR=M4 / RL=M2）。
   * 注意：3D 舞台的标签现在只显示轮位（FL/FR/RR/RL），不再显示 M 编号。 */
  const WHEEL_CHANNEL = { FL: 'M1', FR: 'M2', RR: 'M4', RL: 'M3' };

  /* 辊子倾斜方向（按实车逐轮确认）：
   *   FL(M3) / RR(M2) → 辊子沿「左前 ↔ 右后」这条对角线
   *   FR(M1) / RL(M4) → 辊子沿「右前 ↔ 左后」这条对角线
   * 代码里 tilt.rotation.y = -π/4 得到前一条对角线，+π/4 得到后一条。 */
  const ROLLER_TILT = { FL: -1, RR: -1, FR: 1, RL: 1 };
  /* 地址栏 #flip 可把所有辊子朝向整体镜像一次，用来和实车/照片对照（A/B 切换用） */
  const HASH = (typeof location !== 'undefined' && location.hash) ? location.hash : '';
  const TILT_FLIP = HASH.indexOf('flip') >= 0 ? -1 : 1;

  /* 实车尺寸（按整车图纸，1 世界单位 = 100 mm）
   *   整车 164 × 176 mm（宽 × 长）；轮径 Ø60、麦轮宽约 24、底盘板约 108 × 130 mm。
   *   地面网格 48 格跨 24 单位 → 每格 0.5 单位 = 50 mm，可直接对照实车距离。 */
  const DIM = {
    width: 1.64,
    length: 1.76,
    wheelR: 0.30,
    wheelW: 0.24,
    plateW: 1.08,
    plateL: 1.30,
  };
  const WHEEL_X = DIM.width / 2 - DIM.wheelW / 2;   // 轮心横向 = ±0.68
  const WHEEL_Z = DIM.length / 2 - DIM.wheelR;      // 轮心纵向 = ±0.58（轴距 116 mm）

  /* 真麦克纳姆轮结构（照实车特写建模）：
   *   - 黄色塑料星形轮盘：圆盘 + 一圈齿，齿正好卡在辊子之间的缝隙里
   *   - 黑色橡胶辊子：10 颗，与轮轴成 45°，既随轮体公转、又绕自身轴线自转
   *   - 深灰辐条轮毂 + 金属轴，从侧面看是"自行车轮"那种辐条
   *   左右手轮镜像：FL/RR 一组倾角、FR/RL 另一组，与运动学里的 X 关系一致。 */
  const ROLLER_COUNT = 9;

  function buildWheel(scene, meta, wheelRadius, wheelWidth) {
    const group = new THREE.Group();
    const rollerR = (2 * Math.PI * wheelRadius / ROLLER_COUNT) * 0.34;
    const tiltSign = (ROLLER_TILT[meta.id] || 1) * TILT_FLIP;
    const outer = meta.pos[0] > 0 ? 1 : -1;   // 该轮朝车外的一侧（法兰在另一侧）

    const rollerMat = new THREE.MeshStandardMaterial({ color: SKIN.roller, roughness: 0.82, metalness: 0.1 });
    const plateMat = new THREE.MeshStandardMaterial({
      color: SKIN.plate,
      roughness: 0.44,
      metalness: 0.12,
      emissive: SKIN.plate,
      emissiveIntensity: STAGE_DARK ? 0.12 : 0,
    });
    const hubMat = new THREE.MeshStandardMaterial({ color: SKIN.hub, roughness: 0.52, metalness: SKIN.hubMetal });
    const metalMat = new THREE.MeshStandardMaterial({ color: SKIN.metal, roughness: 0.3, metalness: 0.9 });
    const mats = [rollerMat, plateMat, hubMat, metalMat];
    /* 记录本色：渲染时按"转动/静止"整体提亮或压暗，而不是用 setHex 覆盖掉原色 */
    mats.forEach(function (m) { m.userData.base = m.color.clone(); });

    /* 内外两侧对称：两块黄色星形轮盘（实车内外两面都是黄盘） */
    const faceX = wheelWidth * 0.30;

    // 黄色星形轮盘：用 Shape 挤出真正的"花瓣"轮廓（比圆盘+方块齿更像实车）
    const starShape = new THREE.Shape();
    const rOut = wheelRadius * 0.86;
    const rIn = wheelRadius * 0.74;
    const pts = ROLLER_COUNT * 2;
    for (let i = 0; i < pts; i += 1) {
      const a = (i / pts) * Math.PI * 2;
      const rr = i % 2 === 0 ? rOut : rIn;
      const px = Math.cos(a) * rr;
      const py = Math.sin(a) * rr;
      if (i === 0) starShape.moveTo(px, py);
      else starShape.lineTo(px, py);
    }
    starShape.closePath();
    const plateGeo = new THREE.ExtrudeGeometry(starShape, {
      depth: wheelWidth * 0.26,
      bevelEnabled: false,
      curveSegments: 4,
    });
    plateGeo.translate(0, 0, -wheelWidth * 0.13);
    plateGeo.rotateY(Math.PI / 2);   // 挤出方向 Z → 轮轴 X
    plateGeo.rotateX(Math.PI / ROLLER_COUNT);   // 花瓣对齐辊子之间的缝隙
    [-1, 1].forEach(function (side) {
      const plate = new THREE.Mesh(plateGeo, plateMat);
      plate.position.x = side * faceX;
      plate.castShadow = true;
      group.add(plate);
    });

    // 圆头花瓣：在每片花瓣尖端补一小段圆柱，把尖角变圆（照片里齿是圆头）
    const lobeGeo = new THREE.CylinderGeometry(wheelRadius * 0.095, wheelRadius * 0.095, wheelWidth * 0.26, 12);
    lobeGeo.rotateZ(Math.PI / 2);
    for (let i = 0; i < ROLLER_COUNT; i += 1) {
      const a = (i / ROLLER_COUNT) * Math.PI * 2 + Math.PI / ROLLER_COUNT;   // 与花瓣同角
      const lr = rOut - wheelRadius * 0.03;
      [-1, 1].forEach(function (side) {
        const lobe = new THREE.Mesh(lobeGeo, plateMat);
        lobe.position.set(side * faceX, Math.cos(a) * lr, Math.sin(a) * lr);
        group.add(lobe);
      });
    }
    // 盘面上的凸起圆环（照片里黄盘上有一圈凸边）
    const ringGeo = new THREE.TorusGeometry(wheelRadius * 0.52, wheelRadius * 0.022, 8, 30);
    ringGeo.rotateY(Math.PI / 2);
    [-1, 1].forEach(function (side) {
      const ring = new THREE.Mesh(ringGeo, plateMat);
      ring.position.x = side * (faceX + wheelWidth * 0.13);
      group.add(ring);
    });

    /* 注意：外侧方向是 outer（左侧轮 +x、右侧轮 -x），所有"单侧"零件都必须乘 outer，
     * 否则右侧轮的轮毂会被塞进两块黄盘之间而看不见。 */
    const outFaceX = outer * faceX;
    const inFaceX = -outer * faceX;

    // 外侧面：深灰"自行车辐条"轮毂（细辐条 + 外圈 + 银色轴承）
    const hubX = outer * (faceX + wheelWidth * 0.14);
    const hubRingGeo = new THREE.TorusGeometry(wheelRadius * 0.245, wheelRadius * 0.026, 8, 30);
    hubRingGeo.rotateY(Math.PI / 2);
    const hubRing = new THREE.Mesh(hubRingGeo, hubMat);
    hubRing.position.x = hubX;
    group.add(hubRing);
    const spokeGeo = new THREE.BoxGeometry(wheelWidth * 0.09, wheelRadius * 0.235, 0.014);
    for (let i = 0; i < 12; i += 1) {
      const spokeHolder = new THREE.Group();
      spokeHolder.rotation.x = (i / 12) * Math.PI * 2;
      spokeHolder.position.x = hubX;
      const spoke = new THREE.Mesh(spokeGeo, hubMat);
      spoke.position.y = wheelRadius * 0.135;
      spokeHolder.add(spoke);
      group.add(spokeHolder);
    }
    const bearingGeo = new THREE.CylinderGeometry(wheelRadius * 0.075, wheelRadius * 0.075, wheelWidth * 0.20, 14);
    bearingGeo.rotateZ(Math.PI / 2);
    const bearing = new THREE.Mesh(bearingGeo, metalMat);
    bearing.position.x = hubX;
    group.add(bearing);

    // 内侧面（电机侧）：金属法兰 + 6 颗螺栓 + 伸出轴
    const flangeGeo = new THREE.CylinderGeometry(wheelRadius * 0.30, wheelRadius * 0.30, wheelWidth * 0.10, 20);
    flangeGeo.rotateZ(Math.PI / 2);
    const flange = new THREE.Mesh(flangeGeo, metalMat);
    flange.position.x = inFaceX - outer * wheelWidth * 0.06;
    group.add(flange);
    const boltGeo = new THREE.CylinderGeometry(wheelRadius * 0.05, wheelRadius * 0.05, wheelWidth * 0.10, 8);
    boltGeo.rotateZ(Math.PI / 2);
    for (let i = 0; i < 6; i += 1) {
      const a = (i / 6) * Math.PI * 2;
      const bolt = new THREE.Mesh(boltGeo, metalMat);
      bolt.position.set(
        inFaceX - outer * wheelWidth * 0.12,
        Math.cos(a) * wheelRadius * 0.17,
        Math.sin(a) * wheelRadius * 0.17
      );
      group.add(bolt);
    }
    const axleGeo = new THREE.CylinderGeometry(wheelRadius * 0.085, wheelRadius * 0.085, wheelWidth * 0.62, 14);
    axleGeo.rotateZ(Math.PI / 2);
    const axle = new THREE.Mesh(axleGeo, metalMat);
    axle.position.x = inFaceX - outer * wheelWidth * 0.26;
    group.add(axle);

    // 一圈黑色辊子（45° 倾角）
    const rollerGeo = new THREE.CylinderGeometry(rollerR, rollerR, wheelWidth * 1.04, 14);
    rollerGeo.rotateZ(Math.PI / 2);   // 轴向 → X（辊子轴线）
    const capGeo = new THREE.CylinderGeometry(rollerR * 0.72, rollerR * 0.72, wheelWidth * 0.08, 12);
    capGeo.rotateZ(Math.PI / 2);
    const rollers = [];
    for (let i = 0; i < ROLLER_COUNT; i += 1) {
      const holder = new THREE.Group();
      holder.rotation.x = (i / ROLLER_COUNT) * Math.PI * 2;   // 沿轮缘均匀分布
      const tilt = new THREE.Group();
      tilt.position.y = wheelRadius - rollerR;                // 辊子外缘贴到半径 R
      tilt.rotation.y = tiltSign * Math.PI / 4;              // 45° 倾角（麦轮的关键）
      const roller = new THREE.Mesh(rollerGeo, rollerMat);
      roller.castShadow = true;
      tilt.add(roller);
      // 辊子两端的银灰端盖（侧视时能看到一圈金属亮边）
      [-1, 1].forEach(function (side) {
        const cap = new THREE.Mesh(capGeo, metalMat);
        cap.position.x = side * wheelWidth * 0.50;
        tilt.add(cap);
      });
      holder.add(tilt);
      group.add(holder);
      rollers.push(roller);
    }

    group.position.set(meta.pos[0] * WHEEL_X, wheelRadius, meta.pos[1] * WHEEL_Z);
    scene.add(group);
    const label = labelSprite(meta.label);   // 只显示轮位，不显示 M1~M4
    label.position.set(meta.pos[0] * (WHEEL_X + 0.18), wheelRadius * 2.4, meta.pos[1] * (WHEEL_Z + 0.10));
    scene.add(label);  // 之前只创建没挂到场景上，所以 3D 里看不到 M 编号
    return { group, meta, label, rollers, rollerR, mats: mats };
  }

  function labelSprite(text, opts) {
    const o = opts || {};
    const c = document.createElement('canvas');
    c.width = 256;
    c.height = 128;
    const g = c.getContext('2d');
    g.fillStyle = o.bg || SKIN.labelBg;
    g.strokeStyle = o.border || SKIN.labelBorder;
    g.lineWidth = 6;
    const r = 26;
    g.beginPath();
    g.moveTo(r, 8);
    g.arcTo(248, 8, 248, 120, r);
    g.arcTo(248, 120, 8, 120, r);
    g.arcTo(8, 120, 8, 8, r);
    g.arcTo(8, 8, 248, 8, r);
    g.closePath();
    g.fill();
    g.stroke();
    g.fillStyle = o.color || SKIN.labelText;
    g.font = 'bold 52px "Segoe UI","Microsoft YaHei",sans-serif';
    g.textAlign = 'center';
    g.textBaseline = 'middle';
    g.fillText(text, 128, 66);
    const tex = new THREE.CanvasTexture(c);
    /* 关闭透视缩放：四个标签屏幕尺寸一致，近处的不会撑满舞台 */
    const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
      map: tex,
      transparent: true,
      depthWrite: false,
      depthTest: false,
      sizeAttenuation: false,
    }));
    sprite.scale.set(0.105, 0.0525, 1);
    sprite.renderOrder = 10;
    return sprite;
  }

  function buildCar(scene) {
    const car = new THREE.Group();
    const deckY = DIM.wheelR + 0.06;          // 底盘中心高度（轮半径 + 半板厚）
    // 底盘（深色主题=深灰金属；浅色主题=原白色）
    const bodyMat = new THREE.MeshStandardMaterial({ color: SKIN.body, roughness: 0.46, metalness: 0.42 });
    const body = new THREE.Mesh(new THREE.BoxGeometry(DIM.plateW, 0.12, DIM.plateL), bodyMat);
    body.position.y = deckY;
    body.castShadow = true;
    car.add(body);

    // 上盖（实车中间那块带元件的板）
    const topY = deckY + 0.06 + 0.06;
    const topMat = new THREE.MeshStandardMaterial({ color: SKIN.top, roughness: 0.32, metalness: 0.45 });
    const top = new THREE.Mesh(new THREE.BoxGeometry(DIM.plateW * 0.66, 0.12, DIM.plateL * 0.66), topMat);
    top.position.y = topY;
    top.castShadow = true;
    car.add(top);

    // 驱动板（实车中间那块 PCB）
    const pcbY = topY + 0.06 + 0.02;
    const pcbMat = new THREE.MeshStandardMaterial({ color: SKIN.pcb, roughness: 0.6, metalness: 0.2 });
    const pcb = new THREE.Mesh(new THREE.BoxGeometry(DIM.plateW * 0.58, 0.032, DIM.plateL * 0.55), pcbMat);
    pcb.position.set(0, pcbY, 0.02);
    car.add(pcb);

    // 板载元件（点缀，增强"真板子"的感觉）
    const chipMat = new THREE.MeshStandardMaterial({ color: 0x0d1610, roughness: 0.8 });
    [[-0.17, 0.13], [0.13, 0.18], [0.01, -0.13]].forEach(function (p, i) {
      const chip = new THREE.Mesh(new THREE.BoxGeometry(0.08 + i * 0.02, 0.045, 0.07 + i * 0.02), chipMat);
      chip.position.set(p[0], pcbY + 0.035, p[1]);
      car.add(chip);
    });

    // 语义条（车头方向提示）
    const stripeMat = new THREE.MeshStandardMaterial({ color: SKIN.accent, roughness: 0.3, emissive: SKIN.accent, emissiveIntensity: STAGE_DARK ? 0.55 : 0 });
    const stripe = new THREE.Mesh(new THREE.BoxGeometry(0.26, 0.045, 0.40), stripeMat);
    stripe.position.set(0, pcbY + 0.035, -0.12);
    car.add(stripe);

    // 车头方向由地面箭头表达，不使用锥形指示

    scene.add(car);
    return car;
  }

  /* 地面方向罗盘：正前方发光箭头 + 前/后/左/右 地面标签。
   * 这样相机怎么转都能一眼看出"车头/前进"是哪一边，不再只能靠车头那个小锥体猜。 */
  function buildGroundCompass(scene) {
    const group = new THREE.Group();          // 整组跟着车走（所以箭头/方位标签始终在车周围）
    state.compassGroup = group;
    scene.add(group);
    const accent = SKIN.accent;
    const frontMat = new THREE.MeshBasicMaterial({
      color: accent, transparent: true, opacity: STAGE_DARK ? 0.62 : 0.45, depthWrite: false,
    });
    const rearMat = new THREE.MeshBasicMaterial({
      color: 0x8aa0c0, transparent: true, opacity: 0.26, depthWrite: false,
    });

    // 前方箭头主体（沿 +z = 车头方向）
    const shaft = new THREE.Mesh(new THREE.PlaneGeometry(0.13, 0.58), frontMat);
    shaft.rotation.x = -Math.PI / 2;
    shaft.position.set(0, 0.008, 0.90);
    group.add(shaft);

    // 箭头三角头
    const triShape = new THREE.Shape();
    triShape.moveTo(0, 0.30);
    triShape.lineTo(-0.20, 0);
    triShape.lineTo(0.20, 0);
    triShape.closePath();
    const headGeo = new THREE.ExtrudeGeometry(triShape, { depth: 0.02, bevelEnabled: false, curveSegments: 3 });
    headGeo.rotateX(Math.PI / 2);          // 形状平面 → 地面（尖端朝 +z）
    const head = new THREE.Mesh(headGeo, frontMat);
    head.position.set(0, 0.010, 1.20);
    group.add(head);

    // 后方短淡箭头，提示"后"
    const rear = new THREE.Mesh(new THREE.PlaneGeometry(0.10, 0.42), rearMat);
    rear.rotation.x = -Math.PI / 2;
    rear.position.set(0, 0.008, -0.88);
    group.add(rear);

    // 地面上方的四个方向标签（用屏幕标签保证任何角度都可读，配合地面箭头表达"前"）
    const spots = [
      { text: '前', x: 0, z: 1.58, sz: 0.095, color: '#22d3ee', border: '#22d3ee' },
      { text: '后', x: 0, z: -1.24, sz: 0.078, color: '#c9d8ea', border: 'rgba(157,176,201,0.75)' },
      { text: '左', x: 1.22, z: 0.10, sz: 0.078, color: '#c9d8ea', border: 'rgba(157,176,201,0.75)' },
      { text: '右', x: -1.22, z: 0.10, sz: 0.078, color: '#c9d8ea', border: 'rgba(157,176,201,0.75)' },
    ];
    spots.forEach(function (s) {
      const sp = labelSprite(s.text, { color: s.color, border: s.border });
      sp.position.set(s.x, 0.34, s.z);
      sp.scale.set(s.sz, s.sz / 2, 1);
      group.add(sp);
    });

    return { frontMat: frontMat, rearMat: rearMat, group: group };
  }

  function buildGround(scene) {
    const grid = new THREE.GridHelper(24, 48, THEME.gridMajor, THEME.gridMinor);
    grid.position.y = 0.001;
    if (THEME.gridOpacity < 1) {
      grid.material.transparent = true;
      grid.material.opacity = THEME.gridOpacity;
    }
    scene.add(grid);
    const plane = new THREE.Mesh(
      new THREE.PlaneGeometry(60, 60),
      new THREE.MeshStandardMaterial({ color: THEME.ground, roughness: 1 })
    );
    plane.rotation.x = -Math.PI / 2;
    plane.position.y = -0.002;
    plane.receiveShadow = true;
    scene.add(plane);
    return grid;
  }

  function buildArrow(scene) {
    const mat = new THREE.MeshStandardMaterial({ color: 0x2f6fed, roughness: 0.4 });
    const group = new THREE.Group();
    const shaft = new THREE.Mesh(new THREE.BoxGeometry(0.09, 0.05, 1), mat);
    shaft.position.z = 0.5;
    const head = new THREE.Mesh(new THREE.ConeGeometry(0.16, 0.28, 16), mat);
    head.rotation.x = Math.PI / 2;
    head.position.z = 1.06;
    group.add(shaft, head);
    group.position.y = 0.03;
    group.visible = false;
    if (state.compassGroup) state.compassGroup.add(group); else scene.add(group);
    return group;
  }

  function buildRotationRing(scene) {
    const geom = new THREE.TorusGeometry(0.62, 0.035, 8, 48, Math.PI * 1.5);
    const mat = new THREE.MeshStandardMaterial({ color: 0xf0932b, roughness: 0.4 });
    const ring = new THREE.Mesh(geom, mat);
    ring.rotation.x = -Math.PI / 2;
    ring.position.y = 0.04;
    ring.visible = false;
    scene.add(ring);
    return ring;
  }

  function init(canvas) {
    if (typeof THREE === 'undefined' || !canvas) return false;
    // WebGL 可用性探测（无 GPU 的环境直接回退 2D）
    try {
      const probe = document.createElement('canvas');
      const ok = probe.getContext('webgl') || probe.getContext('experimental-webgl');
      if (!ok) return false;
    } catch (e) {
      return false;
    }

    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(THEME.bg);

    const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 100);

    scene.add(new THREE.HemisphereLight(THEME.hemiSky, THEME.hemiGround, THEME.hemiIntensity));
    const dir = new THREE.DirectionalLight(0xffffff, THEME.dirIntensity);
    dir.position.set(4, 7, 5);
    dir.castShadow = true;
    dir.shadow.mapSize.set(1024, 1024);
    dir.shadow.camera.left = -6;
    dir.shadow.camera.right = 6;
    dir.shadow.camera.top = 6;
    dir.shadow.camera.bottom = -6;
    scene.add(dir);

    const grid = buildGround(scene);
    const compass = buildGroundCompass(scene);
    const car = buildCar(scene);
    const wheels = WHEEL_META.map((meta) => buildWheel(car, meta, DIM.wheelR, DIM.wheelW));
    const arrow = buildArrow(scene);
    const ring = buildRotationRing(scene);

    state.scene = scene;
    state.camera = camera;
    state.renderer = renderer;
    state.canvas = canvas;
    state.carGroup = car;
    state.camTarget = new THREE.Vector3(0, 0.40, 0);
    state.wheels = wheels;
    state.grid = grid;
    state.arrow = arrow;
    state.rotRing = ring;
    state.ready = true;

    bindOrbit(canvas);
    /* 地址栏加 #top 切到正俯视（车头朝上，和官方图纸/M 标注对照用）；#top-flip 可叠加镜像辊子朝向 */
    if (HASH.indexOf('right') >= 0) {
      state.orbit.yaw = -0.88;   // 从车的右前侧看，检查右侧轮的轮毂/辐条
    }
    if (HASH.indexOf('top') >= 0) {
      state.orbit.yaw = Math.PI;
      state.orbit.pitch = 1.52;   // 接近正垂直俯视，避免斜视把辊子斜向看歪
      state.orbit.dist = 7.2;
    }
    resize();
    requestAnimationFrame(loop);
    return true;
  }

  function bindOrbit(canvas) {
    canvas.addEventListener('pointerdown', (e) => {
      state.orbit.dragging = true;
      state.orbit.lx = e.clientX;
      state.orbit.ly = e.clientY;
      canvas.setPointerCapture(e.pointerId);
    });
    canvas.addEventListener('pointermove', (e) => {
      if (!state.orbit.dragging) return;
      const dx = e.clientX - state.orbit.lx;
      const dy = e.clientY - state.orbit.ly;
      state.orbit.lx = e.clientX;
      state.orbit.ly = e.clientY;
      state.orbit.yaw -= dx * 0.008;
      state.orbit.pitch = Math.max(0.25, Math.min(1.35, state.orbit.pitch - dy * 0.006));
    });
    const stop = () => { state.orbit.dragging = false; };
    canvas.addEventListener('pointerup', stop);
    canvas.addEventListener('pointercancel', stop);
    canvas.addEventListener('lostpointercapture', stop);
    canvas.addEventListener('wheel', (e) => {
      state.orbit.dist = Math.max(2.6, Math.min(9, state.orbit.dist + e.deltaY * 0.0025));
      e.preventDefault();
    }, { passive: false });
  }

  function resize() {
    if (!state.ready) return;
    const canvas = state.canvas;
    const rect = canvas.parentElement.getBoundingClientRect();
    const w = Math.max(320, Math.round(rect.width));
    const h = Math.max(240, Math.round(rect.height));
    state.renderer.setSize(w, h, false);
    state.camera.aspect = w / h;
    state.camera.updateProjectionMatrix();
  }

  function resetPose() {
    state.pose.x = 0;
    state.pose.z = 0;
    state.pose.yaw = 0;
    if (state.camTarget) { state.camTarget.x = 0; state.camTarget.z = 0; }
    if (state.carGroup) { state.carGroup.position.x = 0; state.carGroup.position.z = 0; }
    if (state.compassGroup) { state.compassGroup.position.x = 0; state.compassGroup.position.z = 0; }
  }

  function update(status) {
    if (!state.ready || !status) return;
    const wheels = status.wheels || [];
    state.target = {
      vx: Number((status.summary || {}).vx || 0),
      vy: Number((status.summary || {}).vy || 0),
      omega: Number((status.summary || {}).omega || 0),
    };
    for (let i = 0; i < 4; i += 1) {
      const w = wheels[i];
      state.spin[i] = w ? Number(w.value) || 0 : 0;
    }
    const imu = status.imu || {};
    state.imuHeading = (imu.available && typeof imu.heading === 'number') ? Number(imu.heading) : null;
    const isSim = (status.link || {}).mode === 'sim';
    state.isSim = isSim;
  }

  function setActive(active) {
    state.active = active;
    if (state.ready && active) {
      resize();
    }
  }

  const clock = { t: 0 };

  function loop(ts) {
    if (state.ready) {
      const dt = Math.min(0.05, (ts - (state.lastTs || ts)) / 1000 || 0.016);
      state.lastTs = ts;
      if (state.active) render(dt);
    }
    requestAnimationFrame(loop);
  }

  function render(dt) {
    const { camera, scene, renderer } = state;

    // ---- 位姿积分（只做运动跟随，不显示距离）----
    // 用独立的时间基准：即使掉帧（后台标签页）位移也大致跟得上真实速度
    const nowPose = (typeof performance !== "undefined" ? performance.now() : Date.now());
    const dtp = Math.min(0.5, Math.max(0, (nowPose - (state.poseTs || nowPose)) / 1000)) || dt;
    state.poseTs = nowPose;
    const { vx, vy, omega } = state.target;
    const pose = state.pose;
    const SCALE = 0.02;             // 指令百分比 → 场景单位/秒（观感调参）
    // 注意：summary.omega 已经是 °/s（= rot% × 3.0），这里**不能再乘标定系数**，
    // 否则 3D 里转得比真车快（曾出现"真车转 90°、3D 转 180°"）。
    const dirBody = Math.atan2(vx, vy);          // 0 = 车头(+z)，正 = 车的左侧(+x)
    const speedBody = Math.hypot(vx, vy);
    if (speedBody > 0.5 || Math.abs(omega) > 0.5) {
      const worldDir = pose.yaw + dirBody;
      pose.x += Math.sin(worldDir) * speedBody * SCALE * dtp;
      pose.z += Math.cos(worldDir) * speedBody * SCALE * dtp;
    }
    if (state.imuHeading !== null) {
      // 有真机 IMU：航向直接跟实测朝向（全局，闭环跑时 3D 里的朝向就是真实的）
      const targetYaw = THREE.MathUtils.degToRad(state.imuHeading);
      let dYaw = targetYaw - pose.yaw;
      dYaw = Math.atan2(Math.sin(dYaw), Math.cos(dYaw));          // 取最短路径
      if (Math.abs(dYaw) > Math.PI / 2) {
        pose.yaw = targetYaw;                                     // 重新校准/归零造成的跳变：直接对齐
      } else {
        pose.yaw += dYaw * Math.min(1, dtp * 10);                 // 平滑跟随（避免 5Hz 轮询抖动）
      }
    } else if (Math.abs(omega) > 0.5) {
      pose.yaw += THREE.MathUtils.degToRad(omega) * dtp;   // 无 IMU：按指令积分（omega 已是 °/s）
    }
    state.carGroup.position.x = pose.x;
    state.carGroup.position.z = pose.z;
    // 注意：rotation.y 是航向，x/z 仍是俯仰/侧倾（下面会覆盖 x/z）
    // 朝向符号：车模型 +z 为车头、+x 为车的左侧，rotation.y = +yaw 时 +z 转向 +x（左转），
    // 与 IMU 约定（heading 增大 = 左转/逆时针）一致；写 -yaw 会左右镜像，
    // 表现为跑方框时"最后一条边车头朝向是反的"。
    state.carGroup.rotation.y = pose.yaw * state.yawSign;

    // 罗盘组跟随车
    if (state.compassGroup) {
      state.compassGroup.position.x = pose.x;
      state.compassGroup.position.z = pose.z;
      state.compassGroup.rotation.y = pose.yaw * state.yawSign;
    }

    // 相机：fixed = 固定机位看整车在场景里移动；follow = 相机追车（地面看起来在流动）
    const o = state.orbit;
    const tgt = state.camTarget || (state.camTarget = new THREE.Vector3(0, 0.40, 0));
    if (state.cameraMode === 'follow') {
      tgt.x += (pose.x - tgt.x) * Math.min(1, dt * 4);
      tgt.z += (pose.z - tgt.z) * Math.min(1, dt * 4);
    } else {
      // fixed：地面基本不动；只有当车快出画面时才缓慢平移视野（保证一直看得见）
      const LIMIT = 2.6;
      const ex = pose.x - tgt.x;
      const ez = pose.z - tgt.z;
      if (Math.abs(ex) > LIMIT) tgt.x += (ex - Math.sign(ex) * LIMIT) * Math.min(1, dt * 2.5);
      if (Math.abs(ez) > LIMIT) tgt.z += (ez - Math.sign(ez) * LIMIT) * Math.min(1, dt * 2.5);
    }
    camera.position.set(
      tgt.x + Math.sin(o.yaw) * Math.cos(o.pitch) * o.dist,
      0.35 + Math.sin(o.pitch) * o.dist,
      tgt.z + Math.cos(o.yaw) * Math.cos(o.pitch) * o.dist
    );
    camera.lookAt(tgt.x, 0.40, tgt.z);

    // 轮子转动（指令 % → 绕车轴自转）+ 辊子绕自身轴线自转
    state.wheels.forEach((w, i) => {
      const spd = state.spin[i];
      w.group.rotation.x += spd * dt * 0.55;
      const active = Math.abs(spd) > 0;
      const gain = active ? 1.0 : 0.62;
      w.mats.forEach(function (m) { m.color.copy(m.userData.base).multiplyScalar(gain); });
      /* 辊子自转：接触地面时辊子相对轮体反向滚动，速率按 轮半径/辊半径 换算 */
      const rollerAngle = -w.group.rotation.x * (DIM.wheelR / w.rollerR) * 0.68;
      w.rollers.forEach(function (r) { r.rotation.x = rollerAngle; });
    });

    // 车体轻微姿态（加速/横移时倾斜，增强立体感）
    state.phase.vx += (vx - state.phase.vx) * Math.min(1, dt * 6);
    state.phase.vy += (vy - state.phase.vy) * Math.min(1, dt * 6);
    state.carGroup.rotation.z = THREE.MathUtils.degToRad(-state.phase.vx * 0.05);
    state.carGroup.rotation.x = THREE.MathUtils.degToRad(state.phase.vy * 0.05);
    state.carGroup.position.y = 0.01 * Math.sin(ts_or_now() * 0.002) * (Math.abs(vx) + Math.abs(vy) > 0 ? 1 : 0);

    // 地面网格：fixed 模式保持静止（地面不动，车自己走）；follow 模式吸附到车脚下
    if (state.grid) {
      if (state.cameraMode === 'follow') {
        const cell = 24 / 48;                     // grid 尺寸/分段 = 0.5
        state.grid.position.x = Math.round(pose.x / cell) * cell;
        state.grid.position.z = Math.round(pose.z / cell) * cell;
      } else {
        state.grid.position.x = 0;
        state.grid.position.z = 0;
      }
    }
    const speed = Math.hypot(vx, vy);

    // 运动箭头
    if (state.arrow) {
      const vis = speed > 1;
      state.arrow.visible = vis;
      if (vis) {
        const len = Math.min(1.6, 0.35 + speed / 45);
        state.arrow.scale.set(1, 1, len);
        state.arrow.rotation.y = Math.atan2(vx, vy);   // vx 正=车的左侧(+x)，vy 正=车头(+z)
        state.arrow.position.set(-vx * 0.012, 0.05, -vy * 0.012);   // group 内局部坐标
      }
    }

    // 自转弧
    if (state.rotRing) {
      const vis = Math.abs(omega) > 0.5;
      state.rotRing.visible = vis;
      if (vis) {
        clock.t += dt * (omega > 0 ? 2.4 : -2.4);
        state.rotRing.rotation.z = clock.t;
      }
    }

    renderer.render(scene, camera);
  }

  let _nowBase = 0;
  function ts_or_now() {
    if (!_nowBase) _nowBase = performance.now();
    return performance.now() - _nowBase;
  }

  /** 切换航向显示符号：+1 = 模型约定（+x 为车左侧）；-1 = 镜像。用于现场校准视角观感。 */
  function setYawSign(sign) {
    state.yawSign = (Number(sign) < 0) ? -1 : 1;
    return state.yawSign;
  }

  function setCameraMode(mode) {
    state.cameraMode = mode === 'follow' ? 'follow' : 'fixed';
    return state.cameraMode;
  }

  window.Car3D = {
    resetPose: resetPose,
    setCameraMode: setCameraMode,
    setYawSign: setYawSign,
    init,
    update,
    setActive,
    resize,
    get ready() { return state.ready; },
  };
})();
