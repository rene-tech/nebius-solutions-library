// Real browser and read/download-only acceptance. Credentials remain in memory.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const {chromium} = require('/home/tux/.npm/_npx/e41f203b7505f1fb/node_modules/playwright');

const PHASE_OPERATION = '18efdb3c-fa99-42a7-9a39-876d22a2021b';
const PHASE_SECONDS = 3.946846;

function parseChecks(arguments_) {
  const options = {phaseRegression: false, sourceCommit: null, cases: []};
  for (const value of arguments_) {
    if (value === '--phase-regression') options.phaseRegression = true;
    else if (value.startsWith('--source-commit=')) {
      options.sourceCommit = value.slice('--source-commit='.length);
      assert(/^[0-9a-f]{40}$/.test(options.sourceCommit), 'exact 40-character deployed source commit required');
    } else {
      const [model, id, extra] = value.split('=');
      assert(!extra && /^[a-z0-9-]+$/.test(model) && /^[0-9a-f-]{36}$/.test(id), 'model=operation-id expected');
      options.cases.push([model, id]);
    }
  }
  if (options.phaseRegression) {
    assert(options.sourceCommit, 'phase regression requires the root-confirmed deployed source commit');
    assert.equal(options.cases.length, 0, 'phase regression uses only the retained r01 Protenix operation');
    options.cases = [['protenix-v2', PHASE_OPERATION]];
  } else if (!options.cases.length) {
    options.cases = [
      ['rfdiffusion', 'b80a8b11-9015-4dac-8dcb-206225e1baa5'],
      ['protenix-v2', 'e2f0915b-75dd-4673-ab7e-f82c6a551da9'],
      ['rfdiffusion-bulk', '7abc44a6-0759-4825-87a9-447bf227ca96'],
    ];
  }
  return options;
}

function verifyPhaseRegression(body, observed) {
  const duration = body.data.lifecycle_phases.find(item => item.phase === 'restore')?.duration;
  assert(duration, 'restore phase exists');
  assert.equal(duration.unit, 'seconds');
  assert.equal(duration.source, 'lifecycle-signal-boundaries');
  assert.equal(duration.evidence, 'estimated', 'application-observed boundaries retain their evidence quality');
  assert.equal(typeof duration.value, 'number');
  assert(Math.abs(duration.value - PHASE_SECONDS) < 0.0000005, 'restore uses actual 3.946846-second boundaries, not ingestion timestamps');
  assert.match(observed.restoreCard, /\b3\.95s\b/, 'browser renders the rounded restore duration');
  assert.match(observed.restoreCard, /estimated/i);
  assert.match(observed.restoreCard, /lifecycle-signal-boundaries/);
  assert.match(observed.contextLabel, /^Cluster context checked /);
  assert.match(observed.runLabel, /^Run data observed /);
  for (const label of [observed.contextLabel, observed.runLabel]) {
    assert(!/Not observed|Invalid timestamp/.test(label), 'observation labels contain actual timestamps');
    assert(/\d/.test(label), 'observation labels include a timestamp');
  }
  assert(Number.isFinite(Date.parse(body.meta.generated_at)), 'run API observation timestamp is valid');
  assert.match(observed.phaseCopy, /Observed wall-time union per phase/);
  assert.match(observed.phaseCopy, /not additive request latency or GPU-seconds/);
  return {operation_id: PHASE_OPERATION, status: 'passed', expected_seconds: PHASE_SECONDS,
    actual_duration: duration, browser_restore_card: observed.restoreCard,
    context_label: observed.contextLabel, run_label: observed.runLabel,
    api_observed_at: body.meta.generated_at,
    clock: 'Union of actual paired lifecycle signal occurred_at boundaries; independent of GPU count.'};
}

async function main() {
  process.umask(0o077);
  const [credentialPath, output, ...caseArguments] = process.argv.slice(2);
  assert(credentialPath && output, 'credential bundle and new output directory are required');
  const checks = parseChecks(caseArguments);
  assert(!fs.existsSync(path.join(output, 'report.json')), 'use a new evidence directory; prior reports are immutable');
  fs.mkdirSync(output, {recursive: true, mode: 0o700});
  fs.mkdirSync(path.join(output, 'output/playwright'), {recursive: true, mode: 0o700});
  const credentials = JSON.parse(fs.readFileSync(credentialPath)).credentials;
  const origin = 'https://89.169.99.188';
  const report = {schema: 'fs2-serve.nebius.ai/admin-remediation-preflight/v1',
    started_at: new Date().toISOString(), status: 'running',
    source_commit: checks.sourceCommit,
    source_identity_basis: 'Caller-supplied release identity; deployment is verified separately by the release owner.',
    scope: 'Existing bootstrap operator; browser, authorized reads and downloads only.',
    expected_initial_session_401: true, errors: [], browser_console: [], failed_requests: [],
    page_resources: [], runs: [], downloads: []};
  function diagnosticText(value) {
    let text = String(value);
    for (const secret of Object.values(credentials)) {
      if (typeof secret === 'string' && secret.length > 12) text = text.replaceAll(secret, '[redacted]');
    }
    return text.slice(0, 4096);
  }
  function save(name, value) {
    const text = JSON.stringify(value, null, 2) + '\n';
    for (const secret of Object.values(credentials)) {
      if (typeof secret === 'string' && secret.length > 12) assert(!text.includes(secret), 'secret in evidence');
    }
    fs.writeFileSync(path.join(output, name), text, {mode: 0o600});
  }
  const browser = await chromium.launch({headless: true, executablePath: '/usr/bin/google-chrome'});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}, acceptDownloads: true,
    locale: 'en-US', timezoneId: 'UTC'});
  const page = await context.newPage();
  page.on('pageerror', error => report.errors.push({at: new Date().toISOString(), kind: 'pageerror',
    name: error.name, message: diagnosticText(error.message)}));
  page.on('console', item => {
    if (['error', 'warning'].includes(item.type())) report.browser_console.push({at: new Date().toISOString(),
      type: item.type(), text: diagnosticText(item.text())});
  });
  page.on('requestfailed', request => report.failed_requests.push({at: new Date().toISOString(),
    path: new URL(request.url()).pathname, type: request.resourceType(),
    failure: diagnosticText(request.failure()?.errorText ?? 'unknown')}));
  page.on('response', response => {
    if (['document', 'script', 'stylesheet'].includes(response.request().resourceType())) {
      const headers = response.headers();
      report.page_resources.push({at: new Date().toISOString(), path: new URL(response.url()).pathname,
        status: response.status(), type: response.request().resourceType(),
        content_type: headers['content-type'] ?? null, content_length: headers['content-length'] ?? null});
    }
    if (response.status() >= 400) report.errors.push({at: new Date().toISOString(), kind: 'http',
      status: response.status(), path: new URL(response.url()).pathname});
  });
  async function snapshot(label) {
    const record = {at: new Date().toISOString(), url: page.url(), snapshot: await page.locator('body').ariaSnapshot()};
    save(label + '.json', record);
    await page.screenshot({path: path.join(output, 'output/playwright', label + '.png'), fullPage: true});
    return record.snapshot;
  }
  async function api(endpoint, label) {
    const started = performance.now();
    const response = await context.request.get(origin + endpoint);
    const raw = await response.body();
    save(label + '.json', {at: new Date().toISOString(), endpoint, status: response.status(),
      duration_seconds: (performance.now() - started) / 1000, body: JSON.parse(raw)});
    assert.equal(response.status(), 200, `${endpoint} status`);
    return JSON.parse(raw);
  }
  try {
    await page.goto(origin + '/admin/scientific-runs');
    await snapshot('login-page');
    await page.getByRole('textbox', {name: 'Bootstrap access token'}).fill(credentials.admin_bootstrap_token);
    await page.getByRole('button', {name: 'Sign in', exact: true}).click();
    await page.getByRole('navigation').first().waitFor();
    report.signed_in_at = new Date().toISOString();
    for (const [model, id] of checks.cases) {
      await page.goto(origin + '/admin/scientific-runs/' + id);
      await page.getByRole('heading', {name: 'Phase durations', exact: true}).waitFor();
      const body = await api('/admin/api/v1/scientific-runs/' + id, model + '-api');
      if (checks.phaseRegression) {
        const section = page.locator('section[aria-labelledby="scientific-lifecycle-title"]');
        const restoreCard = section.locator('article').filter({has: page.getByText('restore', {exact: true})});
        await restoreCard.waitFor();
        report.phase_regression = verifyPhaseRegression(body, {
          restoreCard: await restoreCard.innerText(),
          contextLabel: await page.locator('.generated-at').innerText(),
          runLabel: await page.getByText(/^Run data observed /).innerText(),
          phaseCopy: await section.innerText(),
        });
      }
      const text = await snapshot(model + '-detail');
      assert.equal(body.data.run.status, 'succeeded');
      assert.equal(body.data.semantic_validation.status, 'passed');
      const artifactSource = body.meta.sources.find(item => item.id === 'scientific-artifacts');
      assert.equal(artifactSource.state, 'available');
      assert(artifactSource.age_seconds <= 90);
      assert(!text.includes('Stale data'));
      const accounting = body.data.run.gpu_accounting;
      for (const key of ['allocated', 'active', 'idle_total', 'grace_drain']) {
        assert.equal(typeof accounting[key].value, 'number', key + ' numeric');
        assert(['measured', 'estimated'].includes(accounting[key].evidence));
      }
      const delta = accounting.allocated.value - accounting.active.value - accounting.idle_total.value - accounting.grace_drain.value;
      assert(Math.abs(delta) < 0.001, 'GPU occupied phase partition');
      report.runs.push({model, operation_id: id, artifact_source: artifactSource,
        completed_at: body.data.run.completed_at, accounting, partition_delta_gpu_seconds: delta,
        artifact_count: body.data.artifacts.length, status: 'passed'});
      if (model.endsWith('-bulk')) continue;
      const artifact = body.data.artifacts.find(item => item.role === 'output' && /pdb|mmcif|cif/.test(item.media_type));
      assert(artifact, 'actual scientific structure artifact is listed');
      assert(artifact.download.available && artifact.download.href.startsWith('/admin/'));
      const link = page.locator('a').filter({hasText: 'Download artifact'}).and(page.locator(`a[href="${artifact.download.href}"]`));
      const started = performance.now();
      const downloadPromise = page.waitForEvent('download');
      await link.click();
      const download = await downloadPromise;
      assert.equal(await download.failure(), null);
      const file = await download.path();
      const bytes = fs.readFileSync(file);
      const sha256 = crypto.createHash('sha256').update(bytes).digest('hex');
      assert.equal('sha256:' + sha256, artifact.sha256);
      assert.equal(bytes.length, artifact.size_bytes.value);
      report.downloads.push({model, operation_id: id, artifact_id: artifact.artifact_id,
        media_type: artifact.media_type, suggested_filename: download.suggestedFilename(),
        bytes: bytes.length, sha256, browser_download_seconds: (performance.now() - started) / 1000,
        status: 'passed'});
      await download.delete();
    }
    if (!checks.phaseRegression) {
    await page.goto(origin + '/admin/scientific-runs');
    await page.getByRole('heading', {name: 'Scientific model readiness', exact: true}).waitFor();
    const science = await api('/admin/api/v1/scientific-models', 'scientific-models-api');
    const qualified = ['rfdiffusion', 'protenix-v2', 'esmfold2', 'esmfold2-fast'];
    for (const id of qualified) {
      const model = science.data.items.find(item => item.model_id === id);
      assert.equal(model.caching.gpu_snapshot, 'verified', id + ' selectable capability');
      assert.equal(model.caching.exact_tier, 'not-observed');
    }
    await page.getByText('GPU snapshot available as an option', {exact: false}).first().waitFor();
    const scienceText = await snapshot('scientific-model-capabilities');
    assert(scienceText.includes('available as an option'));
    report.snapshot_options = {models: qualified, status: 'passed', distinguishes_capability_from_run_observation: true};
    await page.goto(origin + '/admin/overview');
    await page.getByRole('heading', {name: 'Completed requests', exact: true}).waitFor();
    const overview = await api('/admin/api/v1/overview', 'overview-api');
    const overviewText = await snapshot('overview-aligned-counters');
    assert(overviewText.includes('Exact completed_at window'));
    assert(overviewText.includes('replicas deduplicated'));
    const reconciliation = overview.data.reconciliation;
    assert.equal(typeof reconciliation.durable_terminal_operations.value, 'number');
    assert.equal(typeof reconciliation.prometheus_terminal_operations.value, 'number');
    assert.equal(reconciliation.prometheus_terminal_operations.state, 'estimated');
    assert.equal(reconciliation.difference.value,
      reconciliation.prometheus_terminal_operations.value - reconciliation.durable_terminal_operations.value);
    report.overview = {context: overview.meta.context, reconciliation, status: 'passed'};
    }
    const unexpected = report.errors.filter(item => !(item.status === 401 && item.path === '/admin/api/v1/session'));
    assert.deepEqual(unexpected, [], 'no unexpected browser/server errors');
    assert.deepEqual(report.failed_requests, [], 'no failed browser requests');
    report.status = 'passed';
  } catch (error) {
    report.status = 'failed';
    report.failure = {type: error.name, message: error.message};
    await snapshot('failure-page').catch(() => {});
    process.exitCode = 1;
  } finally {
    report.completed_at = new Date().toISOString();
    await browser.close();
    report.browser_closed_at = new Date().toISOString();
    save('report.json', report);
    console.log(JSON.stringify(report));
  }
}
module.exports = {parseChecks, verifyPhaseRegression, PHASE_OPERATION, PHASE_SECONDS};
if (require.main === module) {
  main().catch(error => {console.error(JSON.stringify({status: 'harness-failed', type: error.name})); process.exitCode = 1;});
}
