// Small native Web Audio adapter, not a Pipecat/RTVI implementation.
export class PCMPlayback {
  constructor(Context = globalThis.AudioContext) {
    this.Context = Context; this.context = null; this.sources = new Set();
    this.nextTime = 0; this.enabled = false;
  }
  async enable() {
    if (!this.Context) throw new Error('Live audio is unavailable in this browser; replay retained WAV recordings.');
    this.context ||= new this.Context();
    await this.context.resume();
    if (this.context.state !== 'running') throw new Error('Press Enable live audio to allow browser playback.');
    this.enabled = true;
  }
  chunk(event) {
    if (!this.enabled) return false;
    if (this.context?.state !== 'running') throw new Error('Browser audio is suspended. Press Enable live audio.');
    const rate = event.sample_rate_hz;
    if (event.encoding !== 'pcm_s16le' || event.channels !== 1 || !Number.isInteger(rate) || rate < 8000 || rate > 96000) throw new Error('Unsupported live audio format.');
    const raw = atob(event.audio_base64);
    if (!raw.length || raw.length % 2 || raw.length > 16384) throw new Error('Invalid live PCM chunk.');
    if (this.bufferedSeconds > 240) throw new Error('Live playback fell behind. Replay the retained recording.');
    const buffer = this.context.createBuffer(1, raw.length / 2, rate), samples = buffer.getChannelData(0);
    for (let i = 0; i < samples.length; i++) {
      const value = raw.charCodeAt(i * 2) | raw.charCodeAt(i * 2 + 1) << 8;
      samples[i] = (value >= 32768 ? value - 65536 : value) / 32768;
    }
    const source = this.context.createBufferSource(); source.buffer = buffer;
    source.connect(this.context.destination); this.sources.add(source);
    source.onended = () => { this.sources.delete(source); source.disconnect(); };
    const start = Math.max(this.nextTime, this.context.currentTime + 0.04);
    source.start(start); this.nextTime = start + buffer.duration;
    return true;
  }
  get bufferedSeconds() { return Math.max(0, this.nextTime - (this.context?.currentTime || 0)); }
  stop() {
    for (const source of this.sources) { try { source.stop(); source.disconnect(); } catch { /* already ended */ } }
    this.sources.clear(); this.nextTime = 0;
  }
  async close() {
    this.stop(); this.enabled = false;
    if (this.context) await this.context.close();
    this.context = null;
  }
}
