// Весь звук проходит через браузер: только так встроенное эхоподавление WebRTC знает, что играет Джарвис.

export class MicCapture {
  constructor({ onFrame, onLevel }) {
    this.onFrame = onFrame;
    this.onLevel = onLevel;
    this.stream = null;
    this.ctx = null;
    this.echoMode = 'нет';
  }

  get active() {
    return this.stream !== null;
  }

  async start(deviceId = '') {
    await this.stop();
    this.stream = await this.#request(deviceId);
    this.ctx = new AudioContext({ sampleRate: 16000, latencyHint: 'playback' });
    await this.ctx.audioWorklet.addModule('/static/mic-worklet.js');
    const source = this.ctx.createMediaStreamSource(this.stream);
    // Нет выходов: звук микрофона никуда не воспроизводится, иначе получилась бы петля.
    const node = new AudioWorkletNode(this.ctx, 'jarvis-mic', { numberOfOutputs: 0 });
    let beat = 0;
    node.port.onmessage = ({ data }) => {
      this.onFrame(data.pcm);
      beat += 1;
      if (beat % 2 === 0) this.onLevel(data.rms);
    };
    source.connect(node);
    await this.ctx.resume();
    const settings = this.stream.getAudioTracks()[0]?.getSettings?.() ?? {};
    this.echoMode = String(settings.echoCancellation ?? 'нет');
  }

  async #request(deviceId) {
    const base = { channelCount: 1, noiseSuppression: true, autoGainControl: true };
    if (deviceId) base.deviceId = { exact: deviceId };
    try {
      // Значение "all" просит убирать весь звук системы, а не только WebRTC-звонки.
      return await navigator.mediaDevices.getUserMedia({
        audio: { ...base, echoCancellation: { ideal: 'all' } },
      });
    } catch (error) {
      if (error.name !== 'OverconstrainedError' && error.name !== 'TypeError') throw error;
      return navigator.mediaDevices.getUserMedia({ audio: { ...base, echoCancellation: true } });
    }
  }

  async stop() {
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = null;
    const ctx = this.ctx;
    this.ctx = null;
    if (ctx) await ctx.close().catch(() => {});
  }
}

const SILENCE = 0.004;

// Обработка голоса под Джарвиса: чуть больше груди и разборчивости, короткие задержки дают металлический отзвук, как из
// динамика в шлеме, а маленький отклик добавляет пространство. Всё очень тихо: речь должна оставаться естественной.
class ArmorVoice {
  constructor(ctx) {
    this.ctx = ctx;
    this.input = ctx.createGain();
    this.output = ctx.createGain();
    this.dry = ctx.createGain();
    this.wet = ctx.createGain();
    this.input.connect(this.dry);
    this.dry.connect(this.output);

    const chest = this.#filter('lowshelf', 150, 3.2);
    const box = this.#filter('peaking', 340, -2.4, 1);
    const clarity = this.#filter('peaking', 3100, 2.6, 0.9);
    const edge = this.#filter('highshelf', 8200, -2);
    this.input.connect(chest);
    chest.connect(box);
    box.connect(clarity);
    clarity.connect(edge);
    edge.connect(this.wet);
    this.wet.connect(this.output);

    for (const [seconds, level] of [[0.0071, 0.16], [0.0113, 0.1], [0.0187, 0.06]]) {
      const delay = ctx.createDelay(0.05);
      delay.delayTime.value = seconds;
      const gain = ctx.createGain();
      gain.gain.value = level;
      edge.connect(delay);
      delay.connect(gain);
      gain.connect(this.wet);
    }
    const room = ctx.createConvolver();
    room.buffer = this.#roomResponse();
    const roomLevel = ctx.createGain();
    roomLevel.gain.value = 0.11;
    edge.connect(room);
    room.connect(roomLevel);
    roomLevel.connect(this.wet);
    this.set(true);
  }

  #filter(type, frequency, gain, q = 0.7) {
    const filter = this.ctx.createBiquadFilter();
    filter.type = type;
    filter.frequency.value = frequency;
    filter.gain.value = gain;
    filter.Q.value = q;
    return filter;
  }

  #roomResponse() {
    // Затухающий шум вместо записанного помещения: не нужен лишний файл, а хвост короткий и не мешает разборчивости.
    const seconds = 0.32;
    const length = Math.floor(this.ctx.sampleRate * seconds);
    const buffer = this.ctx.createBuffer(1, length, this.ctx.sampleRate);
    const data = buffer.getChannelData(0);
    for (let i = 0; i < length; i += 1) {
      data[i] = (Math.random() * 2 - 1) * Math.exp((-7 * i) / length);
    }
    return buffer;
  }

  set(on) {
    this.dry.gain.value = on ? 0 : 1;
    this.wet.gain.value = on ? 0.92 : 0;
  }
}

function trimSilence(buffer, sentenceEnd) {
  // Сервис добавляет тишину по краям фразы. Если её не срезать, стыки фрагментов звучат как запинки.
  const data = buffer.getChannelData(0);
  const rate = buffer.sampleRate;
  const step = Math.max(1, Math.floor(rate / 4000));
  let head = 0;
  while (head < data.length && Math.abs(data[head]) < SILENCE) head += step;
  let tail = data.length - 1;
  while (tail > head && Math.abs(data[tail]) < SILENCE) tail -= step;
  if (tail <= head) return { offset: 0, duration: buffer.duration };
  const start = Math.max(0, head / rate - 0.03);
  const end = Math.min(buffer.duration, tail / rate + (sentenceEnd ? 0.26 : 0.09));
  return { offset: start, duration: end - start };
}

export class VoicePlayer {
  constructor({ onChunkStart, onTurnStart, onTurnDone, onIdle }) {
    this.onChunkStart = onChunkStart;
    this.onTurnStart = onTurnStart;
    this.onTurnDone = onTurnDone;
    this.onIdle = onIdle;
    this.ctx = new AudioContext({ latencyHint: 'playback' });
    this.gain = this.ctx.createGain();
    this.analyser = this.ctx.createAnalyser();
    this.analyser.fftSize = 512;
    this.analyser.smoothingTimeConstant = 0.72;
    this.armor = new ArmorVoice(this.ctx);
    this.gain.connect(this.armor.input);
    this.armor.output.connect(this.analyser);
    this.analyser.connect(this.ctx.destination);
    this.fft = new Uint8Array(this.analyser.frequencyBinCount);
    this.wave = new Float32Array(this.analyser.fftSize);
    this.bands = new Uint8Array(64);
    this.sources = new Set();
    this.chain = Promise.resolve();
    this.generation = 0;
    this.nextTime = 0;
    this.turn = null;
    this.timers = new Set();
    this.history = [];
  }

  get playing() {
    return this.sources.size > 0;
  }

  setEffect(on) {
    this.armor.set(on !== false);
  }

  // Режим playback копит звук перед выводом: анализатор видит его раньше, чем он слышен.
  get outputDelay() {
    return (this.ctx.outputLatency || 0) + (this.ctx.baseLatency || 0);
  }

  async resume() {
    if (this.ctx.state !== 'running') await this.ctx.resume().catch(() => {});
    return this.ctx.state === 'running';
  }

  feed(header, payload) {
    const generation = this.generation;
    if (!this.turn || this.turn.id !== header.turn) {
      this.turn = { id: header.turn, expected: null, ended: 0, started: false };
      this.gain.gain.cancelScheduledValues(this.ctx.currentTime);
      this.gain.gain.value = 1;
    }
    const turn = this.turn;
    // Цепочка промисов сохраняет порядок: короткий фрагмент может декодироваться быстрее длинного.
    this.chain = this.chain.then(async () => {
      if (generation !== this.generation) return;
      try {
        const buffer = await this.ctx.decodeAudioData(payload);
        if (generation === this.generation) this.#schedule(buffer, header, turn);
      } catch (error) {
        console.warn('Не удалось декодировать звук', error);
        this.#chunkEnded(turn);
      }
    });
  }

  finish(turnId, count) {
    if (this.turn && this.turn.id === turnId) {
      this.turn.expected = count;
      this.#maybeDone(this.turn);
    } else {
      this.onTurnDone(turnId);
    }
  }

  #schedule(buffer, header, turn) {
    const sentenceEnd = /[.!?…]["»)]*\s*$/.test(header.text ?? '');
    const { offset, duration } = trimSilence(buffer, sentenceEnd);
    const now = this.ctx.currentTime;
    const when = Math.max(this.nextTime, now + 0.03);
    const source = this.ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(this.gain);
    source.onended = () => {
      this.sources.delete(source);
      this.#chunkEnded(turn);
      if (this.sources.size === 0) this.onIdle();
    };
    source.start(when, offset, duration);
    this.sources.add(source);
    this.nextTime = when + duration;
    const timer = setTimeout(() => {
      this.timers.delete(timer);
      if (!turn.started) {
        turn.started = true;
        this.onTurnStart(turn.id);
      }
      this.onChunkStart(header, duration);
    }, (when - now + this.outputDelay) * 1000);
    this.timers.add(timer);
  }

  #chunkEnded(turn) {
    turn.ended += 1;
    this.#maybeDone(turn);
  }

  #maybeDone(turn) {
    if (turn.expected !== null && turn.ended >= turn.expected && !turn.done) {
      turn.done = true;
      this.onTurnDone(turn.id);
    }
  }

  stop() {
    this.generation += 1;
    this.chain = Promise.resolve();
    this.timers.forEach(clearTimeout);
    this.timers.clear();
    for (const source of this.sources) {
      source.onended = null;
      try {
        source.stop();
      } catch {
        // Источник мог уже закончиться сам.
      }
    }
    this.sources.clear();
    this.nextTime = 0;
    this.turn = null;
    this.onIdle();
  }

  duck(on) {
    const now = this.ctx.currentTime;
    this.gain.gain.cancelScheduledValues(now);
    this.gain.gain.setTargetAtTime(on ? 0.2 : 1, now, on ? 0.03 : 0.12);
  }

  // Вызывается каждым кадром анимации, пока идёт речь: сжимает спектр до 64 полос.
  sample() {
    this.analyser.getByteFrequencyData(this.fft);
    this.analyser.getFloatTimeDomainData(this.wave);
    let energy = 0;
    for (let i = 0; i < this.wave.length; i += 1) energy += this.wave[i] * this.wave[i];
    const rms = Math.sqrt(energy / this.wave.length);
    const bins = Math.floor(this.fft.length * 0.42);
    for (let band = 0; band < 64; band += 1) {
      const low = Math.floor((band / 64) ** 1.6 * bins);
      const high = Math.max(low + 1, Math.floor(((band + 1) / 64) ** 1.6 * bins));
      let peak = 0;
      for (let bin = low; bin < high; bin += 1) peak = Math.max(peak, this.fft[bin]);
      this.bands[band] = peak;
    }
    // Показываем то, что человек слышит сейчас, а не то, что только что ушло в буфер вывода.
    const now = performance.now();
    this.history.push({ at: now, level: Math.min(1, rms * 4), bands: this.bands.slice() });
    const due = now - this.outputDelay * 1000;
    while (this.history.length > 1 && this.history[1].at <= due) this.history.shift();
    return this.history[0];
  }

  chime(kind) {
    if (this.ctx.state !== 'running') return;
    const now = this.ctx.currentTime;
    const tones = kind === 'timer' ? [[659, 0], [880, 0.14], [1175, 0.28]] : [[740, 0], [1109, 0.07]];
    for (const [freq, delay] of tones) {
      const osc = this.ctx.createOscillator();
      const env = this.ctx.createGain();
      osc.type = 'sine';
      osc.frequency.value = freq;
      env.gain.setValueAtTime(0, now + delay);
      env.gain.linearRampToValueAtTime(0.07, now + delay + 0.012);
      env.gain.exponentialRampToValueAtTime(0.0001, now + delay + 0.22);
      osc.connect(env);
      env.connect(this.ctx.destination);
      osc.start(now + delay);
      osc.stop(now + delay + 0.24);
    }
  }
}
