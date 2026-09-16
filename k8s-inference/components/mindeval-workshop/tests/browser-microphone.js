// Requires a logged-in rehearsal session and Chromium fake microphone configured
// with a known synthetic speech WAV. This tests the real browser capture path,
// not merely a direct WebSocket client. Never use real patient recordings here.
async page => {
  const evidence = {kind: 'browser-microphone', checks: []};
  const submit = async (button, suffix, expected = 200) => {
    const pending = page.waitForResponse(r => r.url().endsWith(suffix) && r.request().method() === 'POST');
    await button.click();
    const response = await pending;
    if (response.status() !== expected) throw new Error(`${suffix}: HTTP ${response.status()}`);
    return response.json();
  };
  await page.locator('#mode').selectOption('canonical');
  await page.locator('#profiles').selectOption('profile-012');
  await page.locator('#clinicians').selectOption('Qwen/Qwen3-30B-A3B-Instruct-2507');
  await page.locator('#rounds').fill('2');
  await page.locator('#tokens').fill('4096');
  const created = await submit(page.getByRole('button', {name: 'Start evaluation', exact: true}), '/v1/workshop/runs', 202);
  evidence.run_id = created.data[0].id;
  await page.waitForFunction(id => document.querySelector('#run-status').textContent.includes(id), evidence.run_id);
  const path = `/v1/workshop/runs/${evidence.run_id}/interventions`;
  await submit(page.getByRole('button', {name: 'Pause', exact: true}), path);
  await page.locator('#role').selectOption('patient');
  await submit(page.getByRole('button', {name: 'Take over', exact: true}), path);
  await page.waitForFunction(() => document.querySelector('#run-status').textContent.startsWith('takeover'), null, {timeout: 45000});
  await page.getByRole('button', {name: 'Use microphone', exact: true}).click();
  await page.waitForFunction(() => document.querySelector('#mic-status').textContent.startsWith('Recording ·'), null, {timeout: 30000});
  // Fixed audio capture duration is the input fixture, not a page-readiness wait.
  await page.waitForTimeout(5000);
  await page.getByRole('button', {name: 'Finish recording', exact: true}).click();
  await page.waitForFunction(() => document.querySelector('#mic-status').textContent.includes('submitted and retained'), null, {timeout: 45000});
  evidence.checks.push('microphone permission, PCM capture and public WebSocket completed');
  const aborted = await submit(page.getByRole('button', {name: 'Abort', exact: true}), path);
  const human = aborted.state.transcript.find(turn => turn.human && turn.source === 'microphone');
  if (!human || !human.content?.trim() || !human.audio_url) throw new Error('Human audio/transcript was not retained');
  evidence.transcript = human.content;
  evidence.audio_url = human.audio_url;
  evidence.checks.push('recognized patient text and WAV retained with microphone provenance');
  evidence.passed = aborted.status === 'aborted' && aborted.state.intervened;
  await page.evaluate(value => { window.workshopMicrophoneAcceptance = value; }, evidence);
  return evidence;
}
