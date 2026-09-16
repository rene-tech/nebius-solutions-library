// Requires a logged-in rehearsal session and Chromium fake microphone configured
// with a known synthetic speech WAV. This tests the real browser capture path,
// not merely a direct WebSocket client. Never use real patient recordings here.
// To resume an already-selected patient takeover without creating a new run, set
// window.workshopMicrophoneResume = {run_id, prior_queue_timeout_ms} first.
async page => {
  const resume = await page.evaluate(() => window.workshopMicrophoneResume || null);
  const evidence = {kind: 'browser-microphone', checks: [], started_at: new Date().toISOString(),
    input: 'preconfigured Chromium synthetic fake microphone, not a physical microphone'};
  const submit = async (button, suffix, expected = 200) => {
    const pending = page.waitForResponse(r => r.url().endsWith(suffix) && r.request().method() === 'POST');
    await button.click();
    const response = await pending;
    if (response.status() !== expected) throw new Error(`${suffix}: HTTP ${response.status()}`);
    return response.json();
  };
  if (resume) {
    if (!/^[0-9a-f-]{36}$/.test(resume.run_id)) throw new Error('Invalid resume run ID');
    evidence.run_id = resume.run_id;
    evidence.resumed_same_run = true;
    evidence.prior_attempt = {stage: 'wait for queued patient takeover', outcome: 'timeout',
      timeout_ms: resume.prior_queue_timeout_ms, load_context: '60-job load', source: 'manager-reported original attempt'};
    if (!await page.locator('#run-status').textContent().then(text => text.includes(evidence.run_id))) {
      throw new Error('Resume requires the same selected existing run');
    }
    if (await page.locator('#role').inputValue() !== 'patient') throw new Error('Resume requires patient takeover');
  } else {
    await page.locator('#mode').selectOption('canonical');
    await page.locator('#profiles').selectOption('profile-012');
    await page.locator('#clinicians').selectOption('Qwen/Qwen3-30B-A3B-Instruct-2507');
    await page.locator('#rounds').fill('2');
    await page.locator('#tokens').fill('4096');
    const created = await submit(page.getByRole('button', {name: 'Start evaluation', exact: true}), '/v1/workshop/runs', 202);
    evidence.run_id = created.data[0].id;
  }
  await page.waitForFunction(id => document.querySelector('#run-status').textContent.includes(id), evidence.run_id);
  const path = `/v1/workshop/runs/${evidence.run_id}/interventions`;
  if (!resume) {
    await submit(page.getByRole('button', {name: 'Pause', exact: true}), path);
    await page.locator('#role').selectOption('patient');
    await submit(page.getByRole('button', {name: 'Take over', exact: true}), path);
  }
  await page.waitForFunction(() => document.querySelector('#run-status').textContent.startsWith('takeover'), null, {timeout: 45000});
  evidence.takeover_ready_at = new Date().toISOString();
  await page.getByRole('button', {name: 'Use microphone', exact: true}).click();
  await page.waitForFunction(() => document.querySelector('#mic-status').textContent.startsWith('Recording ·'), null, {timeout: 30000});
  const captureStarted = Date.now();
  // Fixed audio capture duration is the input fixture, not a page-readiness wait.
  await page.waitForTimeout(5000);
  evidence.capture_wait_ms = Date.now() - captureStarted;
  await page.getByRole('button', {name: 'Finish recording', exact: true}).click();
  await page.waitForFunction(() => document.querySelector('#mic-status').textContent.includes('submitted and retained'), null, {timeout: 45000});
  evidence.checks.push('microphone permission, PCM capture and public WebSocket completed');
  const aborted = await submit(page.getByRole('button', {name: 'Abort', exact: true}), path);
  const human = aborted.state.transcript.find(turn => turn.human && turn.source === 'microphone');
  if (!human || !human.content?.trim() || !human.audio_url) throw new Error('Human audio/transcript was not retained');
  evidence.transcript = human.content;
  evidence.audio_url = human.audio_url;
  await page.evaluate(value => { window.workshopMicrophoneAcceptance = value; }, evidence);
  const recording = page.waitForResponse(r => r.url().endsWith(human.audio_url) && r.request().method() === 'GET');
  await page.locator('#transcript .turn').filter({hasText: 'human'}).getByRole('button', {name: 'Replay recording', exact: true}).first().click();
  const replay = await recording;
  if (replay.status() !== 200) throw new Error(`Retained replay HTTP ${replay.status()}`);
  // Reuse the owner's existing request authorization in memory only. No header
  // value is returned, logged or written into evidence.
  const authorization = await replay.request().headerValue('authorization');
  evidence.audio = await page.evaluate(async ({url, run, authorization}) => {
    if (!url.startsWith(`/v1/workshop/runs/${run}/audio/`)) throw new Error('Unexpected retained audio path');
    const response = await fetch(url, {headers: {Authorization: authorization}});
    if (!response.ok) throw new Error(`Retained audio HTTP ${response.status}`);
    const bytes = await response.arrayBuffer();
    const view = new DataView(bytes);
    const tag = offset => String.fromCharCode(...new Uint8Array(bytes, offset, 4));
    if (bytes.byteLength < 44 || tag(0) !== 'RIFF' || tag(8) !== 'WAVE'
      || view.getUint32(4, true) + 8 !== bytes.byteLength) throw new Error('Incomplete WAV');
    let format, channels, rate, bits, dataOffset, dataBytes;
    for (let offset = 12; offset + 8 <= bytes.byteLength;) {
      const size = view.getUint32(offset + 4, true);
      if (offset + 8 + size > bytes.byteLength) throw new Error('Truncated WAV chunk');
      if (tag(offset) === 'fmt ') {
        if (size < 16) throw new Error('Invalid WAV format');
        format = view.getUint16(offset + 8, true); channels = view.getUint16(offset + 10, true);
        rate = view.getUint32(offset + 12, true); bits = view.getUint16(offset + 22, true);
      }
      if (tag(offset) === 'data') {dataOffset = offset + 8; dataBytes = size;}
      offset += 8 + size + size % 2;
    }
    if (format !== 1 || channels !== 1 || rate !== 16000 || bits !== 16 || !dataBytes || dataBytes % 2) {
      throw new Error('Unexpected microphone WAV format');
    }
    let peak = 0;
    for (let offset = dataOffset; offset < dataOffset + dataBytes; offset += 2) {
      peak = Math.max(peak, Math.abs(view.getInt16(offset, true)));
    }
    if (!peak) throw new Error('Retained microphone WAV is silent');
    const sha = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), value => value.toString(16).padStart(2, '0')).join('');
    return {http_status: response.status, bytes: bytes.byteLength, sha256: sha, sample_rate_hz: rate,
      channels, bits_per_sample: bits, audio_seconds: dataBytes / (rate * channels * bits / 8), pcm_peak: peak};
  }, {url: human.audio_url, run: evidence.run_id, authorization});
  await page.waitForFunction(() => {
    const audio = document.querySelector('#transcript audio');
    return audio && (audio.error || (audio.readyState >= 2 && Number.isFinite(audio.duration) && audio.duration > 0));
  }, null, {timeout: 15000});
  evidence.browser_media = await page.evaluate(() => {
    const audio = document.querySelector('#transcript audio');
    return {duration_seconds: audio.duration, ready_state: audio.readyState, error_code: audio.error?.code || null};
  });
  if (evidence.browser_media.error_code) throw new Error('Browser WAV decoding failed');
  evidence.checks.push('recognized patient text and WAV retained with microphone provenance');
  evidence.passed = aborted.status === 'aborted' && aborted.state.intervened;
  evidence.final_status = aborted.status;
  evidence.completed_at = new Date().toISOString();
  await page.evaluate(value => { window.workshopMicrophoneAcceptance = value; }, evidence);
  return evidence;
}
