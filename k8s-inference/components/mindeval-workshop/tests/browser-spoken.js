// Logged-in synthetic rehearsal session only. Exercises actual Web Audio,
// public WebSocket delivery, pause/barge-in, resume, retained audio and judgment.
async page => {
  const evidence = {kind: 'browser-spoken', checks: [], events: []};
  const check = (condition, message) => {
    if (!condition) throw new Error(message);
    evidence.checks.push(message);
  };
  await page.evaluate(() => {
    window.workshopAudioProbe = {started: 0, stopped: 0, nonSilent: 0};
    if (!window.workshopAudioInstrumented) {
      window.workshopAudioInstrumented = true;
      const original = AudioContext.prototype.createBufferSource;
      AudioContext.prototype.createBufferSource = function (...args) {
        const source = original.apply(this, args), start = source.start.bind(source), stop = source.stop.bind(source);
        source.start = (...values) => {
          window.workshopAudioProbe.started++;
          if (source.buffer?.getChannelData(0).some(value => Math.abs(value) > 0.001)) window.workshopAudioProbe.nonSilent++;
          return start(...values);
        };
        source.stop = (...values) => { window.workshopAudioProbe.stopped++; return stop(...values); };
        return source;
      };
    }
  });
  page.on('websocket', socket => {
    if (!socket.url().endsWith('/playback')) return;
    socket.on('framereceived', ({payload}) => {
      try {
        const event = JSON.parse(payload.toString());
        // Incoming public metadata only. Never capture outgoing auth messages.
        if (event.type !== 'audio.chunk' || !evidence.events.some(e => e.type === 'audio.chunk')) {
          evidence.events.push({type: event.type, at: Date.now(), role: event.role, stream_id: event.stream_id});
        }
      } catch { /* Not an application JSON frame. */ }
    });
  });
  await page.locator('#mode').selectOption('spoken');
  await page.locator('#profiles').selectOption('profile-013');
  await page.locator('#clinicians').selectOption('Qwen/Qwen3-30B-A3B-Instruct-2507');
  await page.locator('#rounds').fill('2');
  await page.locator('#tokens').fill('4096');
  const accepted = page.waitForResponse(r => r.url().endsWith('/v1/workshop/runs') && r.request().method() === 'POST');
  await page.getByRole('button', {name: 'Start evaluation', exact: true}).click();
  const response = await accepted;
  check(response.status() === 202, 'spoken run accepted through browser');
  evidence.run_id = (await response.json()).data[0].id;
  await page.waitForFunction(() => window.workshopAudioProbe.started >= 5, null, {timeout: 180000});
  check(evidence.events.some(e => e.type === 'audio.chunk'), 'live PCM arrived through public playback socket');
  check(!evidence.events.some(e => e.type === 'audio.end'), 'playback began before complete turn audio');
  const paused = page.waitForResponse(r => r.url().endsWith('/interventions') && r.request().method() === 'POST');
  await page.getByRole('button', {name: 'Pause', exact: true}).click();
  check((await paused).status() === 200, 'pause accepted during live speech');
  const stopped = await page.evaluate(() => window.workshopAudioProbe.stopped);
  check(stopped > 0, 'barge-in stopped scheduled browser audio');
  await page.getByRole('button', {name: 'Resume model', exact: true}).click();
  await page.waitForFunction(() => /^(completed|failed|aborted)/.test(document.querySelector('#run-status').textContent), null, {timeout: 900000});
  const completed = (await page.locator('#run-status').textContent()).startsWith('completed');
  check(completed, 'spoken evaluation completed after resume');
  const reportResponse = page.waitForResponse(r => r.url().endsWith(`/runs/${evidence.run_id}/report`));
  const download = page.waitForEvent('download');
  await page.getByRole('button', {name: 'Download evidence', exact: true}).click();
  const report = await (await reportResponse).json();
  await (await download).saveAs(`output/playwright/spoken-${evidence.run_id}.json`);
  const turns = report.run.state.transcript.filter(turn => !turn.seed);
  check(turns.length === 4, 'all four requested spoken turns retained');
  check(turns.every(turn => turn.generated_content && turn.content && turn.audio_segments?.length), 'generated text, recognized text and ordered recordings retained for every turn');
  check(report.run.state.benchmark_eligible === false, 'spoken/intervened result excluded from text benchmark');
  check(Object.keys(report.run.state.judgment?.judgment || {}).length === 5, 'complete five-axis judgment retained');
  evidence.audio_probe = await page.evaluate(() => window.workshopAudioProbe);
  evidence.segment_count = turns.reduce((sum, turn) => sum + turn.audio_segments.length, 0);
  evidence.passed = true;
  await page.evaluate(value => { window.workshopSpokenAcceptance = value; }, evidence);
  return evidence;
}
