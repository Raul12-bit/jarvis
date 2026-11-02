// Реактор Джарвиса на голом WebGL2: один полноэкранный шейдер и немного частиц.
// Плавность держится на трёх вещах. Кадры рисуем только на целых делителях частоты экрана: 40 или 50 кадров
// на мониторе 60 Гц идут неровно, даже если средняя цифра хорошая. Все сглаживания считаются от реального
// времени кадра, а не от их номера. Скорость вращения меняется плавно, а не скачком при смене состояния.

const QUAD_VS = `#version 300 es
in vec2 aPos;
void main() { gl_Position = vec4(aPos, 0.0, 1.0); }`;

const RING_FS = `#version 300 es
precision highp float;
uniform vec2 uRes;
uniform float uTime, uEnergy, uVoice, uMic, uSpin, uChase, uMicGate;
uniform vec3 uColA, uColB;
uniform sampler2D uFft;
out vec4 frag;
const float TAU = 6.28318530718;

float band(float d, float w, float aa) { return 1.0 - smoothstep(w, w + aa, abs(d)); }
float halo(float d, float k) { return exp(-abs(d) * k); }

float triangle(vec2 p, float r) {
  const float k = 1.7320508;
  p.x = abs(p.x) - r;
  p.y = p.y + r / k;
  if (p.x + k * p.y > 0.0) p = vec2(p.x - k * p.y, -k * p.x - p.y) / 2.0;
  p.x -= clamp(p.x, -2.0 * r, 0.0);
  return -length(p) * sign(p.y);
}

void main() {
  float side = min(uRes.x, uRes.y);
  vec2 p = (gl_FragCoord.xy - 0.5 * uRes) / (0.5 * side);
  float r = length(p);
  if (r > 1.0) { frag = vec4(0.0); return; }
  float aa = 2.0 / side;
  float a = atan(p.y, p.x);
  float u = a / TAU + 0.5;
  vec3 col = vec3(0.0);

  // Внешняя шкала: в покое приборная разметка, когда Джарвис говорит — спектр его голоса.
  float n = 96.0;
  float su = fract(u + uSpin * 0.0025);
  float id = floor(su * n);
  float cell = fract(su * n) - 0.5;
  float fft = texture(uFft, vec2(abs(fract(id / n) * 2.0 - 1.0), 0.5)).r;
  fft = fft * sqrt(fft);
  float major = step(mod(id, 8.0), 0.5);
  float breathe = uMic * 0.016 * (0.5 + 0.5 * sin(id * 1.7 + uTime * 5.0));
  float len = 0.007 + major * 0.011 + fft * 0.098 + breathe;
  float base = 0.862;
  float across = abs(cell) * TAU / n * r;
  float bar = (1.0 - smoothstep(0.0031, 0.0031 + aa, across))
    * smoothstep(base - aa, base, r) * (1.0 - smoothstep(base + len, base + len + aa, r));
  float lit = clamp(fft * 1.9 + uMic * 0.55, 0.0, 1.0);
  col += bar * mix(uColA * (0.22 + 0.2 * major), mix(uColA, uColB, fft) * 1.55, lit);
  col += uColA * halo(r - base - 0.03, 38.0) * (0.012 + 0.03 * uEnergy + 0.18 * fft);
  col += uColA * 0.17 * band(r - 0.846, 0.0009, aa);

  // Сегменты данных: неровные куски кольца, узнаваемый рисунок интерфейса Джарвиса. Длина зависит от номера куска, а не от времени.
  float dataU = fract(u - uSpin * 0.016);
  float dataId = floor(dataU * 28.0);
  float dataSeed = fract(sin(dataId * 91.7) * 4375.85);
  float dataF = fract(dataU * 28.0);
  float dataOn = step(0.32, dataSeed) * smoothstep(0.0, 0.03, dataF) * (1.0 - smoothstep(0.42 + 0.52 * dataSeed, 0.46 + 0.52 * dataSeed, dataF));
  col += uColA * dataOn * band(r - 0.925, 0.0046, aa) * (0.16 + 0.42 * uEnergy + 0.5 * uVoice);
  col += uColA * 0.13 * band(r - 0.945, 0.0008, aa);
  // Четыре визирные метки держат взгляд в центре: так кольца читаются как прибор, а не как украшение.
  float cross = (1.0 - smoothstep(0.003, 0.003 + aa, min(abs(p.x), abs(p.y)))) * smoothstep(0.952, 0.958, r) * (1.0 - smoothstep(0.99, 0.996, r));
  col += uColA * cross * (0.28 + 0.3 * uEnergy);
  // Развёртка: слабый луч идёт по кругу, пока Джарвис слушает.
  float sweep = fract(u - uTime * 0.11);
  col += uColA * sweep * sweep * sweep * sweep * uMicGate * 0.05 * smoothstep(0.545, 0.6, r) * (1.0 - smoothstep(0.84, 0.86, r));

  // Три длинные дуги и встречная пунктирная дорожка.
  float arc = fract((u - uSpin * 0.03) * 3.0);
  float arcMask = smoothstep(0.0, 0.007, arc) * (1.0 - smoothstep(0.62, 0.628, arc));
  float arcD = r - 0.812;
  col += mix(uColA, uColB, uChase) * arcMask * (0.2 + 0.72 * uEnergy) * (band(arcD, 0.0024, aa) + 0.3 * halo(arcD, 85.0));
  float dashU = fract((u + uSpin * 0.045) * 64.0);
  float dash = smoothstep(0.42, 0.47, dashU) * (1.0 - smoothstep(0.76, 0.81, dashU));
  col += uColA * (0.07 + 0.13 * uEnergy) * dash * band(r - 0.772, 0.003, aa);

  // Комета на дорожке: Джарвис обрабатывает запрос.
  if (uChase > 0.01) {
    float head = fract(u - uTime * 0.62);
    float comet = head * head * head * head * head * head * head * head;
    comet *= comet;
    col += uColB * comet * uChase * (2.4 * band(r - 0.772, 0.0042, aa) + 0.7 * halo(r - 0.772, 65.0));
  }

  // Кольцо из двенадцати катушек — узнаваемый элемент дугового реактора.
  float coilU = fract(u + uSpin * 0.01);
  float coilN = 12.0;
  float coilId = floor(coilU * coilN);
  float coilF = fract(coilU * coilN) - 0.5;
  float inner = 0.585;
  float outer = 0.705;
  float across2 = (r - inner) / (outer - inner);
  float aaR = aa / (outer - inner);
  float aaU = aa / max(TAU * r, 0.01) * coilN;
  float coil = smoothstep(0.0, aaR, across2) * (1.0 - smoothstep(1.0 - aaR, 1.0, across2))
    * (1.0 - smoothstep(0.415, 0.415 + aaU, abs(coilF)));
  if (coil > 0.0) {
    float bevel = 0.5 + 0.5 * cos((across2 - 0.5) * 3.14159);
    float side2 = 0.72 + 0.28 * cos(coilF * 6.2);
    float windings = 0.8 + 0.2 * sin(across2 * 62.0);
    float chase = fract(coilId / coilN - uTime * 0.78);
    chase = chase * chase * chase;
    float coilFft = texture(uFft, vec2(abs(fract(coilId / coilN) * 2.0 - 1.0), 0.5)).r;
    float light = 0.05 + 0.21 * uEnergy + 1.05 * coilFft * uVoice + uChase * chase * 1.1 + 0.2 * uMic;
    col += mix(uColA, uColB, 0.2 + 0.55 * uChase) * coil * bevel * side2 * windings * light;
  }
  col += uColA * (0.12 + 0.1 * uEnergy) * (band(r - outer, 0.001, aa) + band(r - inner, 0.001, aa));
  col += uColA * halo(r - 0.645, 8.5) * (0.008 + 0.034 * uEnergy + 0.15 * uVoice);

  // Внутреннее кольцо и треугольник вокруг плазмы.
  col += uColA * 0.32 * band(r - 0.545, 0.0014, aa);
  if (r < 0.58) {
    float angle = -uSpin * 0.018;
    mat2 rot = mat2(cos(angle), -sin(angle), sin(angle), cos(angle));
    float tri = triangle(rot * p, 0.442);
    vec3 triColor = mix(uColA, vec3(1.0), 0.28);
    col += triColor * (band(tri, 0.0021, aa) * (0.12 + 0.42 * uEnergy + 0.6 * uVoice)
      + halo(tri, 58.0) * (0.015 + 0.085 * uEnergy + 0.34 * uVoice));

    // Плазма: плавно текущее поле вместо шума, поэтому движется как жидкость, а не мерцает.
    float t = uTime * (0.32 + 0.85 * uEnergy + 0.5 * uVoice);
    vec2 q = p * 3.1;
    float w1 = sin(q.x * 1.3 + t) + sin(q.y * 1.7 - t * 0.8);
    float w2 = sin((q.x + q.y) * 1.9 + w1 * 1.15 + t * 1.25);
    float plasma = 0.5 + 0.28 * w2 + 0.22 * sin(length(q + w1 * 0.33) * 3.4 - t * 1.9);
    float falloff = exp(-r * r * (13.5 - 6.5 * uVoice));
    float power = 0.2 + 0.42 * uEnergy + 1.3 * uVoice + 0.55 * uMic;
    vec3 tint = mix(uColA, vec3(1.0), clamp(0.2 + 0.62 * falloff + 0.3 * uVoice, 0.0, 1.0));
    col += tint * falloff * power * (0.5 + 0.62 * plasma);
    float ripple = sin(r * 44.0 - uTime * 9.0) * exp(-r * 5.2);
    col += uColA * ripple * ripple * uVoice * 0.2;
    col += vec3(1.0) * exp(-r * r * 125.0) * (0.2 + 0.32 * uEnergy + 0.95 * uVoice);
  }

  col = 1.0 - exp(-col * 1.45);
  // Лёгкий шум убирает полосы на плавном свечении.
  col += (fract(sin(dot(gl_FragCoord.xy, vec2(12.9898, 78.233))) * 43758.5453) - 0.5) / 255.0;
  frag = vec4(col, clamp(max(col.r, max(col.g, col.b)) * 1.3, 0.0, 1.0));
}`;

const POINT_VS = `#version 300 es
in vec4 aData;
uniform float uTime, uEnergy, uVoice, uMic, uSpin, uPoint;
out float vLight;
void main() {
  vec3 n = aData.xyz;
  float seed = aData.w;
  float t = uTime * (0.14 + 0.32 * uEnergy);
  float wave = sin(n.x * 2.4 + t * 1.3) * sin(n.y * 2.9 - t) * sin(n.z * 2.1 + t * 0.7);
  float amp = 0.07 + 0.12 * uEnergy + 0.72 * uVoice + 0.32 * uMic;
  // Случайная глубина ломает видимую решётку сферы: получается облако, а не сетка точек.
  float depth = 0.55 + 0.45 * sqrt(fract(seed * 7.31));
  float radius = 0.36 * depth * (1.0 + amp * wave) + 0.022 * sin(seed * 40.0 + t * 2.0);
  vec3 q = n * radius;
  float ca = cos(uSpin * 0.085), sa = sin(uSpin * 0.085);
  q = vec3(ca * q.x + sa * q.z, q.y, -sa * q.x + ca * q.z);
  const float tiltC = 0.913, tiltS = 0.408;
  q = vec3(q.x, tiltC * q.y - tiltS * q.z, tiltS * q.y + tiltC * q.z);
  float persp = 1.75 / (1.75 - q.z);
  gl_Position = vec4(q.xy * persp, 0.0, 1.0);
  gl_PointSize = uPoint * (0.6 + 0.7 * persp) * (0.6 + 0.8 * seed);
  vLight = clamp(0.28 + 1.0 * (q.z + 0.34), 0.06, 1.05);
}`;

const POINT_FS = `#version 300 es
precision highp float;
in float vLight;
uniform vec3 uColA;
uniform float uEnergy, uVoice;
out vec4 frag;
void main() {
  vec2 c = gl_PointCoord - 0.5;
  float a = exp(-dot(c, c) * 16.0);
  vec3 tint = mix(uColA, vec3(1.0), clamp(vLight * 0.38 + uVoice * 0.25, 0.0, 1.0));
  float v = a * vLight * (0.07 + 0.19 * uEnergy + 0.3 * uVoice);
  frag = vec4(tint * v, v);
}`;

const STATES = {
  sleep: { energy: 0.14, speed: 0.24, a: [0.3, 0.68, 0.9], b: [0.28, 0.52, 0.7], chase: 0, fps: 30 },
  muted: { energy: 0.05, speed: 0.08, a: [0.36, 0.46, 0.54], b: [0.3, 0.38, 0.44], chase: 0, fps: 20 },
  listening: { energy: 0.46, speed: 0.46, a: [0.4, 0.86, 1.0], b: [0.56, 0.9, 1.0], chase: 0, fps: 60 },
  hearing: { energy: 0.68, speed: 0.7, a: [0.62, 0.95, 1.0], b: [0.5, 0.89, 1.0], chase: 0, fps: 60 },
  thinking: { energy: 0.54, speed: 2.1, a: [1.0, 0.74, 0.34], b: [1.0, 0.6, 0.2], chase: 1, fps: 60 },
  speaking: { energy: 0.8, speed: 0.8, a: [0.42, 0.87, 1.0], b: [0.88, 0.96, 1.0], chase: 0, fps: 60 },
  error: { energy: 0.25, speed: 0.14, a: [1.0, 0.42, 0.36], b: [1.0, 0.42, 0.36], chase: 0, fps: 30 },
};
// Частицы мягкие и большие, поэтому дороги для встроенной графики: замер показал рост с 5 до 18 % на кадрах 72/с.
const POINTS_FULL = 800;
const POINTS_IDLE = 320;
const POINTS_SAVER = 320;

function compile(gl, type, source) {
  const shader = gl.createShader(type);
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    throw new Error(gl.getShaderInfoLog(shader) ?? 'Ошибка шейдера');
  }
  return shader;
}

function program(gl, vs, fs, attribute) {
  const prog = gl.createProgram();
  gl.attachShader(prog, compile(gl, gl.VERTEX_SHADER, vs));
  gl.attachShader(prog, compile(gl, gl.FRAGMENT_SHADER, fs));
  gl.bindAttribLocation(prog, 0, attribute);
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
    throw new Error(gl.getProgramInfoLog(prog) ?? 'Ошибка программы');
  }
  const uniforms = {};
  const count = gl.getProgramParameter(prog, gl.ACTIVE_UNIFORMS);
  for (let i = 0; i < count; i += 1) {
    const { name } = gl.getActiveUniform(prog, i);
    uniforms[name] = gl.getUniformLocation(prog, name);
  }
  return { prog, uniforms };
}

function fibonacciSphere(count) {
  // Равномерная сфера со случайным числом для разнобоя в движении.
  const data = new Float32Array(count * 4);
  const golden = Math.PI * (3 - Math.sqrt(5));
  // Порядок перемешан: тогда любое начало массива это тоже равномерная сфера, а не её верхняя шапка.
  const order = Array.from({ length: count }, (_, i) => i);
  for (let i = count - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    [order[i], order[j]] = [order[j], order[i]];
  }
  for (let slot = 0; slot < count; slot += 1) {
    const i = order[slot];
    const y = 1 - ((i + 0.5) / count) * 2;
    const radius = Math.sqrt(1 - y * y);
    const theta = golden * i;
    data[slot * 4] = Math.cos(theta) * radius;
    data[slot * 4 + 1] = y;
    data[slot * 4 + 2] = Math.sin(theta) * radius;
    data[slot * 4 + 3] = Math.random();
  }
  return data;
}

// Сглаживание, не зависящее от частоты кадров: за одно и то же время одна и та же доля пути.
const ease = (rate, dt) => 1 - Math.exp(-rate * dt);

export class ReactorCore {
  constructor(canvas, box, wrap) {
    this.canvas = canvas;
    this.box = box;
    this.wrap = wrap;
    this.gl = canvas.getContext('webgl2', {
      alpha: true, premultipliedAlpha: true, antialias: false, depth: false, stencil: false,
      desynchronized: false, powerPreference: 'low-power',
    });
    if (!this.gl) throw new Error('WebGL2 недоступен');
    this.state = 'sleep';
    this.powerSaver = false;
    this.params = { energy: 0.1, speed: STATES.sleep.speed, a: [...STATES.sleep.a], b: [...STATES.sleep.b], chase: 0 };
    this.spin = Math.random() * 100;
    this.time = Math.random() * 50;
    this.voice = 0;
    this.mic = 0;
    this.micTarget = 0;
    this.gate = 0;
    this.sampler = null;
    this.fft = new Uint8Array(64);
    this.smooth = new Float32Array(64);
    this.fftDirty = false;
    this.last = 0;
    this.raf = 0;
    this.frameCount = 0;
    this.vsync = 0;
    this.stride = 2;
    this.refresh = 60;
    this.deltas = [];
    this.lastVsync = 0;
    this.reduceMotion = matchMedia('(prefers-reduced-motion: reduce)');
    this.#init();
    this.resizer = new ResizeObserver(() => this.resize());
    this.resizer.observe(this.wrap);
    this.resize();
    canvas.addEventListener('webglcontextlost', (event) => {
      event.preventDefault();
      cancelAnimationFrame(this.raf);
    });
    canvas.addEventListener('webglcontextrestored', () => {
      this.#init();
      this.start();
    });
  }

  #init() {
    const { gl } = this;
    this.ring = program(gl, QUAD_VS, RING_FS, 'aPos');
    this.points = program(gl, POINT_VS, POINT_FS, 'aData');
    this.quadBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, this.quadBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    this.cloudBuffer = gl.createBuffer();
    this.#fillCloud();
    this.fftTexture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.fftTexture);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R8, 64, 1, 0, gl.RED, gl.UNSIGNED_BYTE, this.fft);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE);
    gl.disable(gl.DEPTH_TEST);
    gl.clearColor(0, 0, 0, 0);
  }

  #fillCloud() {
    const { gl } = this;
    this.pointCount = this.powerSaver ? POINTS_SAVER : POINTS_FULL;
    gl.bindBuffer(gl.ARRAY_BUFFER, this.cloudBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, fibonacciSphere(this.pointCount), gl.STATIC_DRAW);
  }

  setPowerSaver(on) {
    if (this.powerSaver === on) return;
    this.powerSaver = on;
    this.#fillCloud();
    this.resize();
    this.#applyStride();
  }

  setState(state) {
    this.state = STATES[state] ? state : 'sleep';
    this.#applyStride();
  }

  setMic(level) {
    this.micTarget = Math.min(1, level * 7);
  }

  // Короткий всплеск энергии: реактор выходит на режим при запуске. Дальше обычное сглаживание само вернёт его к покою.
  surge() {
    this.params.energy = Math.max(this.params.energy, 1);
    this.spin += 2;
  }

  #applyStride() {
    const wanted = STATES[this.state].fps * (this.powerSaver ? 0.5 : 1);
    // Только целый делитель частоты экрана: иначе кадры идут неравномерно и движение дрожит.
    // Округляем вверх: на экране 144 Гц получится 48 ровных кадров вместо лишних для ноутбука 72.
    this.stride = Math.max(1, Math.ceil(this.refresh / wanted - 0.05));
  }

  #learnRefresh(now) {
    if (this.lastVsync && this.deltas.length < 45) {
      this.deltas.push(now - this.lastVsync);
      if (this.deltas.length === 45) {
        const sorted = [...this.deltas].sort((x, y) => x - y);
        const median = sorted[22];
        if (median > 2 && median < 60) {
          this.refresh = Math.round(1000 / median);
          this.#applyStride();
        }
      }
    }
    this.lastVsync = now;
  }

  resize() {
    const side = Math.max(120, Math.floor(Math.min(this.wrap.clientWidth, this.wrap.clientHeight)));
    this.box.style.width = `${side}px`;
    this.box.style.height = `${side}px`;
    const dpr = Math.min(window.devicePixelRatio || 1, this.powerSaver ? 1 : 1.5);
    const pixels = Math.round(side * dpr);
    const changed = this.canvas.width !== pixels;
    if (changed) {
      this.canvas.width = pixels;
      this.canvas.height = pixels;
    }
    this.pointScale = Math.max(2.6, (pixels / 620) * 7.5);
    // Смена размера стирает холст: без немедленной отрисовки реактор моргал бы при каждом движении края окна.
    if (changed) this.#draw();
  }

  start() {
    cancelAnimationFrame(this.raf);
    this.last = performance.now();
    const tick = (now) => {
      this.raf = requestAnimationFrame(tick);
      this.#learnRefresh(now);
      this.vsync += 1;
      if (this.vsync % this.stride !== 0) return;
      const dt = Math.min((now - this.last) / 1000, 0.1);
      this.last = now;
      this.#update(dt);
      this.#draw();
      this.frameCount += 1;
    };
    this.raf = requestAnimationFrame(tick);
  }

  #update(dt) {
    const target = STATES[this.state];
    const motion = this.reduceMotion.matches ? 0.25 : 1;
    const k = ease(3.4, dt);
    const p = this.params;
    p.energy += (target.energy - p.energy) * k;
    p.chase += (target.chase - p.chase) * k;
    p.speed += (target.speed - p.speed) * ease(2.2, dt);
    for (let i = 0; i < 3; i += 1) {
      p.a[i] += (target.a[i] - p.a[i]) * k;
      p.b[i] += (target.b[i] - p.b[i]) * k;
    }
    this.spin += p.speed * dt * motion * 10;
    this.time += dt * motion;

    let voice = 0;
    const gl = this.gl;
    const sampled = this.sampler ? this.sampler() : null;
    const up = ease(26, dt);
    const down = ease(9, dt);
    let changed = false;
    if (sampled) {
      voice = sampled.level;
      for (let i = 0; i < 64; i += 1) {
        const value = sampled.bands[i];
        this.smooth[i] += (value - this.smooth[i]) * (value > this.smooth[i] ? up : down);
        this.fft[i] = this.smooth[i];
      }
      changed = true;
      this.fftDirty = true;
    } else if (this.fftDirty) {
      let any = 0;
      for (let i = 0; i < 64; i += 1) {
        this.smooth[i] -= this.smooth[i] * down;
        this.fft[i] = this.smooth[i];
        any += this.fft[i];
      }
      changed = true;
      if (any < 8) this.fftDirty = false;
    }
    if (changed) {
      gl.bindTexture(gl.TEXTURE_2D, this.fftTexture);
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, 64, 1, gl.RED, gl.UNSIGNED_BYTE, this.fft);
    }
    this.voice += (voice - this.voice) * ease(voice > this.voice ? 20 : 7, dt);
    const micAllowed = this.state === 'listening' || this.state === 'hearing';
    this.mic += ((micAllowed ? this.micTarget : 0) - this.mic) * ease(14, dt);
    this.gate += ((micAllowed ? 1 : 0) - this.gate) * ease(3, dt);
    this.micTarget *= Math.exp(-dt * 5);
  }

  #common(uniforms) {
    const { gl, params } = this;
    // Лёгкое дыхание: неподвижное свечение выглядит как зависшая картинка.
    const breath = 1 + 0.1 * Math.sin(this.time * 1.15);
    gl.uniform1f(uniforms.uTime, this.time);
    gl.uniform1f(uniforms.uEnergy, params.energy * breath);
    gl.uniform1f(uniforms.uVoice, this.voice);
    gl.uniform1f(uniforms.uMic, this.mic);
    gl.uniform1f(uniforms.uSpin, this.spin);
    gl.uniform3fv(uniforms.uColA, params.a);
    if (uniforms.uColB) gl.uniform3fv(uniforms.uColB, params.b);
  }

  #draw() {
    const { gl, canvas } = this;
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.clear(gl.COLOR_BUFFER_BIT);

    gl.useProgram(this.ring.prog);
    this.#common(this.ring.uniforms);
    gl.uniform2f(this.ring.uniforms.uRes, canvas.width, canvas.height);
    gl.uniform1f(this.ring.uniforms.uChase, this.params.chase);
    gl.uniform1f(this.ring.uniforms.uMicGate, this.gate);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.fftTexture);
    gl.uniform1i(this.ring.uniforms.uFft, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.quadBuffer);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.drawArrays(gl.TRIANGLES, 0, 3);

    gl.useProgram(this.points.prog);
    this.#common(this.points.uniforms);
    gl.uniform1f(this.points.uniforms.uPoint, this.pointScale);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.cloudBuffer);
    gl.vertexAttribPointer(0, 4, gl.FLOAT, false, 0, 0);
    // В покое частицы едва заметны: рисовать все незачем.
    const asleep = this.state === 'sleep' || this.state === 'muted';
    gl.drawArrays(gl.POINTS, 0, asleep ? Math.min(POINTS_IDLE, this.pointCount) : this.pointCount);
  }
}
