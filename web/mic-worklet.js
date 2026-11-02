// Работает в аудиопотоке браузера: копит отсчёты в кадры по 512 (32 мс при 16 кГц), как просит Silero VAD.
class MicProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.frame = new Int16Array(512);
    this.filled = 0;
    this.energy = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;
    for (let i = 0; i < channel.length; i += 1) {
      const sample = Math.max(-1, Math.min(1, channel[i]));
      this.frame[this.filled] = sample < 0 ? sample * 32768 : sample * 32767;
      this.energy += sample * sample;
      this.filled += 1;
      if (this.filled === 512) {
        const pcm = this.frame.buffer.slice(0);
        this.port.postMessage({ pcm, rms: Math.sqrt(this.energy / 512) }, [pcm]);
        this.filled = 0;
        this.energy = 0;
      }
    }
    return true;
  }
}

registerProcessor('jarvis-mic', MicProcessor);
