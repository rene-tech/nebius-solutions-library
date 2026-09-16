// Run in an authenticated, isolated Playwright CLI session with a synthetic
// English speech + trailing-silence fake microphone fixture. Never records keys.
async page => {
  const evidence = {kind: 'browser-automatic-microphone-finish', started_at: new Date().toISOString(), checks: [], policy_events: [], model_events: [], finish_button_clicks: 0};
  const incoming = socket => {
    if (!socket.url().endsWith('/microphone')) return;
    socket.on('framereceived', ({payload}) => {
      try {
        const event = JSON.parse(payload.toString());
        // Do not collect sent frames: the first browser frame contains its key.
        if (event.type?.startsWith('voice_policy.')) evidence.policy_events.push(event);
        else if (['turn.eou', 'turn.eob'].includes(event.type)) evidence.model_events.push(event);
        else if (['workshop.error', 'session.error', 'error'].includes(event.type)) evidence.speech_error = event;
        else if (event.type === 'workshop.message_submitted') evidence.submitted_run = event.run;
      } catch { /* Binary/incomplete frames are not evidence. */ }
    });
  };
  page.on('websocket', incoming);
  const submit = async (button, suffix, expected = 200) => {
    const pending = page.waitForResponse(r => r.url().endsWith(suffix) && r.request().method() === 'POST');
    await button.click();
    const response = await pending;
    if (response.status() !== expected) throw new Error(`${suffix}: HTTP ${response.status()}`);
    return response.json();
  };
  let path;
  try {
    await page.locator('#mode').selectOption('spoken');
    await page.locator('#profiles').selectOption('profile-012');
    await page.locator('#patient').selectOption('Qwen/Qwen3-30B-A3B-Instruct-2507');
    await page.locator('#clinicians').selectOption('Qwen/Qwen3-30B-A3B-Instruct-2507');
    await page.locator('#rounds').fill('2');
    await page.locator('#tokens').fill('4096');
    const created = await submit(page.getByRole('button', {name: 'Start evaluation', exact: true}), '/v1/workshop/runs', 202);
    evidence.run_id = created.data[0].id;
    path = `/v1/workshop/runs/${evidence.run_id}/interventions`;
    await page.waitForFunction(id => document.querySelector('#run-status').textContent.includes(id), evidence.run_id);
    const paused = await submit(page.getByRole('button', {name: 'Pause', exact: true}), path);
    evidence.takeover_role = paused.state.next_role;
    await page.locator('#role').selectOption(evidence.takeover_role);
    await submit(page.getByRole('button', {name: 'Take over', exact: true}), path);
    await page.waitForFunction(() => document.querySelector('#run-status').textContent.startsWith('takeover'));
    if (await page.locator('#auto-finish').isChecked()) throw new Error('Automatic mode is not unchecked by default');
    await page.locator('#auto-finish').check();
    await page.getByRole('button', {name: 'Use microphone', exact: true}).click();
    // No click of Finish recording anywhere in this test.
    await page.waitForFunction(() => [...document.querySelectorAll('#transcript .turn')].some(
      node => node.textContent.includes('human') && node.textContent.includes('Replay recording')
    ), null, {timeout: 60000});
    const human = evidence.submitted_run?.state.transcript.find(turn => turn.human && turn.source === 'microphone');
    if (!human?.content?.trim() || !human.audio_url) throw new Error('No retained human transcript/WAV');
    if (!evidence.policy_events.some(event => event.type === 'voice_policy.input_finish')) throw new Error('No automatic input finish event');
    const ending = evidence.policy_events.find(event => event.type === 'voice_policy.speech_end');
    if (!ending) throw new Error('No structured speech-end source');
    evidence.transcript = human.content;
    evidence.audio_url = human.audio_url;
    evidence.end_source = ending.source;
    evidence.checks.push('browser fake microphone captured PCM', 'automatic input finish without manual Finish click', 'human transcript and WAV reference retained');
    const recording = page.waitForResponse(r => r.url().endsWith(human.audio_url) && r.request().method() === 'GET');
    await page.locator('#transcript .turn').filter({hasText: 'human'}).getByRole('button', {name: 'Replay recording', exact: true}).first().click();
    const response = await recording;
    evidence.replay_response = {
      status: response.status(), content_type: response.headers()['content-type'],
      content_length: response.headers()['content-length'],
    };
    if (response.status() !== 200 || !evidence.replay_response.content_type?.startsWith('audio/wav')) throw new Error('Retained WAV HTTP replay failed');
    // Independently check the durable bytes through APIResponse. Keep the owner's
    // existing authorization in memory only; never include request headers in evidence.
    const verified = await page.request.get(response.url(), {headers: {
      Authorization: await response.request().headerValue('authorization'),
    }});
    const wav = await verified.body();
    evidence.wav_response = {
      status: verified.status(), content_type: verified.headers()['content-type'],
      content_length: verified.headers()['content-length'],
      body_type: Object.prototype.toString.call(wav), first_12_bytes: [...wav.slice(0, 12)],
    };
    if (verified.status() !== 200 || String.fromCharCode(...wav.slice(0, 4)) !== 'RIFF' || String.fromCharCode(...wav.slice(8, 12)) !== 'WAVE' || wav.length <= 44) throw new Error('Retained WAV binary verification failed');
    evidence.wav_bytes = wav.length;
    await page.waitForFunction(() => {
      const audio = document.querySelector('#transcript audio');
      return audio && (audio.error || (audio.readyState >= 2 && Number.isFinite(audio.duration) && audio.duration > 0));
    }, null, {timeout: 15000});
    evidence.browser_media = await page.evaluate(() => {
      const audio = document.querySelector('#transcript audio');
      return {duration_seconds: audio.duration, ready_state: audio.readyState, error_code: audio.error?.code || null};
    });
    if (evidence.browser_media.error_code) throw new Error('Browser failed to decode retained WAV');
    evidence.checks.push('owner-authenticated retained WAV replay returned RIFF/WAVE', 'browser decoded retained audio with positive duration and no media error');
    evidence.passed = true;
  } catch (error) {
    evidence.passed = false;
    evidence.error = error.message;
  } finally {
    if (path) {
      try {
        const aborted = await submit(page.getByRole('button', {name: 'Abort', exact: true}), path);
        evidence.cleanup_status = aborted.status;
        evidence.intervened = aborted.state.intervened;
      } catch (error) { evidence.cleanup_error = error.message; evidence.passed = false; }
    }
    delete evidence.submitted_run;
    evidence.finished_at = new Date().toISOString();
    page.off('websocket', incoming);
    await page.evaluate(value => { window.workshopAutoFinishAcceptance = value; }, evidence);
  }
  return evidence;
}
