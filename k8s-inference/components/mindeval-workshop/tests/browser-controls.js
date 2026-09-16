// Run with playwright-cli run-code --filename tests/browser-controls.js after
// signing into /workshop with a dedicated rehearsal key. Creates one synthetic
// diagnostic run and aborts it; no admin/provider credential is used or saved.
async page => {
  const evidence = {kind: 'browser-controls', checks: []};
  const check = (condition, label) => {
    if (!condition) throw new Error(label);
    evidence.checks.push(label);
  };
  const submit = async (button, suffix) => {
    const response = page.waitForResponse(r => r.url().endsWith(suffix) && r.request().method() === 'POST');
    await button.click();
    const result = await response;
    check(result.status() === (suffix === '/v1/workshop/runs' ? 202 : 200), `${suffix} accepted`);
    return result.json();
  };
  await page.locator('#mode').selectOption('canonical');
  await page.locator('#profiles').selectOption('profile-011');
  await page.locator('#clinicians').selectOption('Qwen/Qwen3-30B-A3B-Instruct-2507');
  await page.locator('#rounds').fill('2');
  await page.locator('#tokens').fill('4096');
  const created = await submit(page.getByRole('button', {name: 'Start evaluation', exact: true}), '/v1/workshop/runs');
  evidence.run_id = created.data[0].id;
  await page.waitForFunction(id => document.querySelector('#run-status').textContent.includes(id), evidence.run_id);
  const path = `/v1/workshop/runs/${evidence.run_id}/interventions`;
  const paused = await submit(page.getByRole('button', {name: 'Pause', exact: true}), path);
  check(paused.status === 'paused', 'pause persisted');
  await page.locator('#message').fill('This is a synthetic workshop control test. Please keep replies concise.');
  await submit(page.getByRole('button', {name: 'Send nudge', exact: true}), path);
  for (const role of ['patient', 'clinician']) {
    await page.locator('#role').selectOption(role);
    const takeover = await submit(page.getByRole('button', {name: 'Take over', exact: true}), path);
    check(takeover.state.takeover_role === role, `${role} takeover persisted`);
    await page.locator('#message').fill(role === 'patient'
      ? 'This is a synthetic patient turn for the workshop. I would like to discuss my sleep.'
      : 'This is a synthetic clinician turn. Thank you for sharing your experience.');
    const said = await submit(page.getByRole('button', {name: 'Speak as selected role', exact: true}), path);
    check(said.state.transcript.some(t => t.role === role && t.human), `${role} typed message retained`);
  }
  const resumed = await submit(page.getByRole('button', {name: 'Resume model', exact: true}), path);
  check(resumed.state.takeover_role == null, 'resume returns role to model');
  const aborted = await submit(page.getByRole('button', {name: 'Abort', exact: true}), path);
  check(aborted.status === 'aborted' && aborted.state.intervened === true, 'abort and intervention label persisted');
  check(!aborted.state.benchmark_eligible, 'intervened run excluded from comparison');
  await page.waitForFunction(() => document.querySelector('#run-status').textContent.startsWith('aborted'));
  evidence.passed = true;
  // Safe non-secret summary retained for a separate eval/screenshot command.
  await page.evaluate(value => { window.workshopAcceptance = value; }, evidence);
  return evidence;
}
