// Bounded real-browser desired-policy check. Run only after root's release GO.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawn, execFileSync} = require('node:child_process');
const {isDeepStrictEqual} = require('node:util');
const {setTimeout: pause} = require('node:timers/promises');

const MODEL = 'cosmos3-nano';
const LABEL = 'Maximum startup retention (seconds)';

function requireCold(spec, view) {
  assert.equal(spec.modelRef, MODEL, 'only Cosmos is authorized');
  assert.equal(spec.availability.minReplicas, 0, 'do not change an existing hot floor');
  assert.equal(view.state, 'observed', 'fresh observed state required');
  assert.equal(view.observation?.status.phase, 'Cold', 'Cosmos is no longer cold; do not mutate');
  for (const field of ['desired', 'ready', 'available']) {
    assert.equal(view.observation.status.replicas?.[field], 0, `Cosmos ${field} replicas must be zero`);
  }
}

function withRetention(original, seconds) {
  const next = structuredClone(original);
  next.availability.startupTimeoutSeconds = seconds;
  return next;
}

function requireOwnedChange(current, original) {
  assert(isDeepStrictEqual(current, original) || isDeepStrictEqual(current, withRetention(original, 1800)),
    'configuration changed outside this check; do not overwrite another operator');
}

async function main() {
  process.umask(0o077);
  const [credentialPath, output, sourceCommit, kubeconfig, contextName, execute] = process.argv.slice(2);
  assert.equal(execute, '--execute', 'explicit root-authorized execution required');
  assert(/^[0-9a-f]{40}$/.test(sourceCommit), 'exact deployed source commit required');
  assert(credentialPath && output && kubeconfig && contextName, 'credentials, new output, kubeconfig and context required');
  assert(!fs.existsSync(output), 'use a new private evidence directory');
  fs.mkdirSync(path.join(output, 'output/playwright'), {recursive: true, mode: 0o700});
  const bundle = JSON.parse(fs.readFileSync(credentialPath));
  const secrets = Object.values(bundle.credentials).filter(value => typeof value === 'string' && value.length > 12);
  const origin = bundle.endpoints.inference_base_url.replace(/\/v1\/?$/, '');
  const report = {schema: 'fs2-serve.nebius.ai/startup-retention-browser-acceptance/v1',
    source_commit: sourceCommit, source_identity_basis: 'Root separately confirms exact rollout before execution.',
    started_at: new Date().toISOString(), model: MODEL, status: 'running',
    actions: [], status_samples: [], pod_events: [], errors: [], failed_requests: []};
  const clean = value => {
    let text = String(value);
    for (const secret of secrets) text = text.replaceAll(secret, '[redacted]');
    return text.slice(0, 4096);
  };
  function save(name, value) {
    const text = JSON.stringify(value, null, 2) + '\n';
    for (const secret of secrets) assert(!text.includes(secret), 'secret in evidence');
    fs.writeFileSync(path.join(output, name), text, {mode: 0o600});
  }
  const {chromium} = require('/home/tux/.npm/_npx/e41f203b7505f1fb/node_modules/playwright');
  const browser = await chromium.launch({headless: true, executablePath: '/usr/bin/google-chrome'});
  const browserContext = await browser.newContext({viewport: {width: 1440, height: 1000}});
  const page = await browserContext.newPage();
  page.on('pageerror', error => report.errors.push({kind: 'pageerror', at: new Date().toISOString(), message: clean(error.message)}));
  page.on('console', item => {
    if (['error', 'warning'].includes(item.type())) report.errors.push({kind: 'console', type: item.type(), at: new Date().toISOString(), message: clean(item.text())});
  });
  page.on('requestfailed', request => report.failed_requests.push({at: new Date().toISOString(), path: new URL(request.url()).pathname, error: clean(request.failure()?.errorText)}));
  let original;
  let apiPath;
  let statusPath;
  let workspace;
  let watch;
  let watchStopped = false;
  let applyAttempted = false;
  let watchExited = null;
  const kubeArgs = ['--kubeconfig', kubeconfig, '--context', contextName, '-n', 'fs2-models'];
  const selector = 'fs2-serve.nebius.ai/model-id=' + MODEL;
  function listPods() {
    const result = JSON.parse(execFileSync('kubectl', [...kubeArgs, 'get', 'pods', '-l', selector, '-o', 'json', '--request-timeout=20s'], {encoding: 'utf8', timeout: 25000, maxBuffer: 4 * 1024 * 1024}));
    assert.equal(result.items.length, 0, 'Cosmos has Pods; stop without activating or deleting anything');
    return result.metadata.resourceVersion;
  }
  async function get(endpoint, label) {
    const response = await browserContext.request.get(origin + endpoint);
    const body = await response.json();
    save(label + '.json', {at: new Date().toISOString(), status: response.status(), body});
    assert.equal(response.status(), 200, label + ' must succeed');
    return body.data;
  }
  async function snapshot(label) {
    save(label + '-browser.json', {at: new Date().toISOString(), url: page.url(), snapshot: await page.locator('body').ariaSnapshot()});
    await page.screenshot({path: path.join(output, 'output/playwright', label + '.png'), fullPage: true});
  }
  async function waitObserved(revision, label) {
    for (let attempt = 0; attempt < 60; attempt++) {
      const view = await get(statusPath, `${label}-status-${attempt}`);
      report.status_samples.push({at: new Date().toISOString(), revision: view.observation?.revision, phase: view.observation?.status.phase, replicas: view.observation?.status.replicas});
      const replicas = view.observation?.status.replicas;
      if (replicas) for (const field of ['desired', 'ready', 'available']) assert.equal(replicas[field], 0, 'no Cosmos activation');
      if (view.state === 'observed' && view.observation?.revision === revision && view.observation.status.phase === 'Cold') return;
      await pause(2000);
    }
    throw new Error('The exact applied revision did not become observed Cold within120 seconds');
  }
  async function applyUi(label, reset) {
    await page.goto(workspace);
    await page.getByLabel(LABEL, {exact: true}).waitFor();
    if (reset) await page.getByRole('button', {name: 'Use default startup retention', exact: true}).click();
    else await page.getByLabel(LABEL, {exact: true}).fill('1800');
    await snapshot(label + '-draft');
    const previewPromise = page.waitForResponse(response => new URL(response.url()).pathname === '/admin/api/v1/model-deployments:plan-preview' && response.request().method() === 'POST');
    await page.getByRole('button', {name: 'Preview render plan', exact: true}).click();
    const preview = await previewPromise;
    save(label + '-preview.json', {status: preview.status(), body: await preview.json()});
    assert.equal(preview.status(), 200, 'browser preview must succeed');
    await page.getByRole('heading', {name: 'Render plan', exact: true}).waitFor();
    const apply = page.getByRole('button', {name: 'Apply', exact: true});
    await apply.waitFor();
    const appliedPromise = page.waitForResponse(response => new URL(response.url()).pathname === '/admin/api/v1/model-deployments:apply' && response.request().method() === 'POST');
    if (!reset) applyAttempted = true;
    await apply.click();
    const response = await appliedPromise;
    const body = await response.json();
    save(label + '-apply.json', {at: new Date().toISOString(), status: response.status(), body});
    assert(response.ok(), 'browser apply must succeed');
    const expected = reset ? original.spec : withRetention(original.spec, 1800);
    const actual = await get(apiPath, label + '-readback');
    assert.deepEqual(actual.spec, expected, 'only the selected startup-retention field may differ');
    report.actions.push({at: new Date().toISOString(), label, status: 'passed', revision: actual.revision, original_spec_restored: reset});
    await snapshot(label + '-applied');
    return actual.revision;
  }
  try {
    await page.goto(origin + '/admin/model-deployments');
    await page.getByRole('textbox', {name: 'Bootstrap access token'}).fill(bundle.credentials.admin_bootstrap_token);
    await page.getByRole('button', {name: 'Sign in', exact: true}).click();
    const row = page.getByRole('row').filter({hasText: MODEL});
    await row.getByRole('link').first().click();
    await page.getByLabel(LABEL, {exact: true}).waitFor();
    workspace = page.url();
    const url = new URL(workspace);
    const name = url.pathname.split('/').pop();
    const params = new URLSearchParams({namespace: url.searchParams.get('namespace') || 'fs2-models', tenant_id: url.searchParams.get('tenant_id') || ''});
    apiPath = '/admin/api/v1/model-deployments/' + encodeURIComponent(name) + '?' + params;
    statusPath = '/admin/api/v1/model-deployments/' + encodeURIComponent(name) + '/status?' + params;
    original = await get(apiPath, 'original');
    assert(original.spec.availability.startupTimeoutSeconds == null, 'original setting is no longer unset; stop without mutation');
    requireCold(original.spec, await get(statusPath, 'original-status'));
    const resourceVersion = listPods();
    await snapshot('original');
    report.pod_watch_started_at = new Date().toISOString();
    watch = spawn('kubectl', [...kubeArgs, 'get', 'pods', '-l', selector, '--watch-only', '--output-watch-events', '--resource-version=' + resourceVersion,
      '-o', 'jsonpath={.type}{"\\t"}{.object.metadata.name}{"\\t"}{.object.metadata.uid}{"\\t"}{.object.status.phase}{"\\n"}', '--request-timeout=600s'], {stdio: ['ignore', 'pipe', 'pipe']});
    let pending = '';
    watch.stdout.on('data', chunk => {
      pending += chunk.toString();
      const lines = pending.split('\n'); pending = lines.pop();
      for (const line of lines.filter(Boolean)) report.pod_events.push({at: new Date().toISOString(), record: clean(line)});
    });
    watch.stderr.on('data', chunk => report.errors.push({kind: 'pod-watch', at: new Date().toISOString(), message: clean(chunk.toString())}));
    watch.on('error', error => { watchExited = {error: clean(error.message)}; });
    watch.on('exit', (code, signal) => { if (!watchStopped) watchExited = {code, signal}; });
    await pause(500);
    assert.equal(watchExited, null, 'Pod watch must be active before mutation');
    await applyUi('override-1800', false);
  } catch (error) {
    report.status = 'failed';
    report.failure = {at: new Date().toISOString(), type: error.name, message: clean(error.message)};
    await snapshot('failure').catch(() => {});
  } finally {
    try {
      if (applyAttempted) {
        const current = await get(apiPath, 'before-restore');
        requireOwnedChange(current.spec, original.spec);
        const revision = isDeepStrictEqual(current.spec, original.spec) ? current.revision : await applyUi('restore-original', true);
        const restored = await get(apiPath, 'restored');
        assert.deepEqual(restored.spec, original.spec, 'exact original configuration must be restored');
        report.original_spec_restored = true;
        await waitObserved(revision, 'restored');
      }
      if (watch) {
        listPods();
        assert.equal(watchExited, null, 'Pod watch must cover the entire mutation window');
        assert.deepEqual(report.pod_events, [], 'no Cosmos Pod may be created or activated');
        report.no_pod_activation = true;
      }
      assert.deepEqual(report.failed_requests, [], 'no failed browser reads or writes');
      assert.deepEqual(report.errors.filter(error => error.kind === 'pageerror'), [], 'no browser application exception');
      if (report.status !== 'failed' && report.original_spec_restored && report.no_pod_activation) report.status = 'passed';
    } catch (error) {
      report.status = 'failed';
      report.cleanup_failure = {at: new Date().toISOString(), type: error.name, message: clean(error.message)};
    }
    if (watch) { watchStopped = true; watch.kill('SIGTERM'); }
    report.pod_watch_stopped_at = new Date().toISOString();
    await browser.close();
    report.browser_closed_at = new Date().toISOString();
    save('report.json', report);
    console.log(JSON.stringify({status: report.status, original_spec_restored: report.original_spec_restored ?? false, no_pod_activation: report.no_pod_activation ?? false, output}));
    if (report.status !== 'passed') process.exitCode = 1;
  }
}

if (require.main === module) main().catch(error => { console.error(JSON.stringify({status: 'harness-failed', type: error.name})); process.exitCode = 1; });
module.exports = {requireCold, withRetention, requireOwnedChange};
