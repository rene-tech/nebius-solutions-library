import assert from 'node:assert/strict';
import test from 'node:test';
import {PCMPlayback} from '../src/fs2_workshop/static/playback.js';

class Context {
  constructor() { this.state = 'suspended'; this.currentTime = 1; this.created = []; this.destination = {}; }
  async resume() { this.state = 'running'; }
  async close() { this.state = 'closed'; }
  createBuffer(channels, samples, rate) { const data = new Float32Array(samples); return {duration: samples / rate, getChannelData: () => data}; }
  createBufferSource() { const source = {connect() {}, disconnect() {}, start(time) { this.at = time; }, stop() { this.stopped = true; }}; this.created.push(source); return source; }
}
const event = {encoding: 'pcm_s16le', channels: 1, sample_rate_hz: 16000, audio_base64: Buffer.from([0, 128, 255, 127]).toString('base64')};
test('first arriving PCM plays immediately; later chunks are contiguous, not whole-WAV buffered', async () => {
  const player = new PCMPlayback(Context); await player.enable();
  assert.equal(player.chunk(event), true);
  assert.equal(player.context.created.length, 1);
  assert.equal(player.context.created[0].at, 1.04);
  assert.deepEqual([...player.context.created[0].buffer.getChannelData(0)], [-1, 32767 / 32768]);
  player.chunk(event);
  assert.equal(player.context.created[1].at, player.context.created[0].at + 2 / 16000);
});
test('barge-in stops every queued source synchronously and resets scheduling', async () => {
  const player = new PCMPlayback(Context); await player.enable(); player.chunk(event); player.chunk(event);
  const sources = [...player.sources]; player.stop();
  assert(sources.every(source => source.stopped)); assert.equal(player.sources.size, 0); assert.equal(player.nextTime, 0);
  player.context.currentTime = 2; player.chunk(event); assert.equal(player.context.created[2].at, 2.04);
});
test('muted audio does not queue and malformed PCM fails explicitly', async () => {
  const player = new PCMPlayback(Context); assert.equal(player.chunk(event), false); await player.enable();
  assert.throws(() => player.chunk({...event, audio_base64: 'AA=='}), /Invalid/);
  assert.throws(() => player.chunk({...event, channels: 2}), /Unsupported/);
  player.nextTime = 500; assert.throws(() => player.chunk(event), /fell behind/);
  await player.close(); assert.equal(player.enabled, false); assert.equal(player.context, null);
});
