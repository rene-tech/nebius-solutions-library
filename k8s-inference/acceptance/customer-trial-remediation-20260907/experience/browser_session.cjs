// Existing real-browser session harness, extended with response capture and exact-byte downloads.
// Only operator sign-in, admin navigation, automatic UI queries, and artifact downloads are allowed.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const readline = require('node:readline');
const assert = require('node:assert/strict');
const {verifyAutomaticPublication} = require('./publication_checks.cjs');
const {chromium} = require('/home/tux/.npm/_npx/e41f203b7505f1fb/node_modules/playwright');

async function main() {
  process.umask(0o077);
  const [credentialPath, output, sourceCommit] = process.argv.slice(2);
  assert(output && !fs.existsSync(output) && /^[a-f0-9]{40}$/.test(sourceCommit), 'new evidence directory and exact source required');
  const credentials = JSON.parse(fs.readFileSync(credentialPath)).credentials;
  fs.mkdirSync(path.join(output, 'output/playwright'), {recursive: true, mode: 0o700});
  const origin = 'https://89.169.99.188';
  const report = {source_commit: sourceCommit, started_at: new Date().toISOString(), errors: [], console: [],
    failed_requests: [], actions: [], downloads: [], publication_checks: [], observed_queries: 0};
  const queries = [];
  const pendingResponses = new Set();
  const redacted = value => {
    let text = String(value);
    for (const secret of Object.values(credentials)) {
      if (typeof secret === 'string' && secret.length > 12) text = text.replaceAll(secret, '[redacted]');
    }
    return text;
  };
  function save(name, value) {
    const text = JSON.stringify(value, null, 2) + '\n';
    assert.equal(redacted(text), text, 'credentials cannot enter evidence');
    fs.writeFileSync(path.join(output, name), text, {mode: 0o600});
  }
  const browser = await chromium.launch({headless: true, executablePath: '/usr/bin/google-chrome'});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}, locale: 'en-US',
    timezoneId: 'UTC', acceptDownloads: true});
  const page = await context.newPage();
  page.on('pageerror', error => report.errors.push({at: new Date().toISOString(), kind: 'pageerror', message: redacted(error.message)}));
  page.on('console', item => {
    if (['error', 'warning'].includes(item.type())) report.console.push({at: new Date().toISOString(), type: item.type(), text: redacted(item.text())});
  });
  page.on('requestfailed', request => report.failed_requests.push({at: new Date().toISOString(),
    path: new URL(request.url()).pathname, failure: request.failure()}));
  page.on('response', response => {
    const pathname = new URL(response.url()).pathname;
    if (response.status() >= 400) report.errors.push({at: new Date().toISOString(), status: response.status(), path: pathname});
    if (!pathname.startsWith('/admin/api/v1/scientific-')) return;
    const pending = (async () => {
      const row = {at: new Date().toISOString(), path: pathname, status: response.status(), body: await response.json()};
      queries.push(row);
      report.observed_queries++;
      save(`query-${String(report.observed_queries).padStart(4, '0')}.json`, row);
    })().catch(error => report.errors.push({at: new Date().toISOString(), kind: 'response-capture', message: redacted(error.message)}));
    pendingResponses.add(pending);
    pending.finally(() => pendingResponses.delete(pending));
  });
  try {
    await page.goto(origin + '/admin/scientific-runs');
    await page.getByRole('textbox', {name: 'Bootstrap access token'}).fill(credentials.admin_bootstrap_token);
    await page.getByRole('button', {name: 'Sign in', exact: true}).click();
    await page.getByRole('navigation').first().waitFor();
    report.signed_in_at = new Date().toISOString();
    console.log(JSON.stringify({status: 'signed-in', at: report.signed_in_at, source_commit: sourceCommit}));
    const input = readline.createInterface({input: process.stdin});
    for await (const line of input) {
      let command;
      try {
        command = JSON.parse(line);
        if (command.action === 'close') break;
        assert(/^[a-zA-Z0-9_-]+$/.test(command.label), 'explicit evidence label required');
        assert(!fs.existsSync(path.join(output, command.label + '.json')), 'evidence labels are immutable');
        if (command.action === 'navigate') {
          assert(/^\/admin\/(scientific-runs(?:\/[0-9a-f-]{36})?|overview|models|capacity)$/.test(command.path), 'read-only admin path required');
          await page.goto(origin + command.path);
          await page.waitForLoadState('networkidle');
        } else if (command.action === 'verify-publication') {
          const id = page.url().split('/').pop();
          const result = verifyAutomaticPublication(queries, report.actions, id);
          await page.locator('section[aria-labelledby="scientific-artifacts-title"]')
            .getByText('Semantic validation passed', {exact: true}).waitFor();
          report.publication_checks.push({...result, at: new Date().toISOString()});
        } else if (command.action === 'download') {
          const id = page.url().split('/').pop();
          const query = queries.findLast(item => item.path === '/admin/api/v1/scientific-runs/' + id && item.status === 200);
          assert(query?.body.data.run.status === 'succeeded', 'visible completed run required');
          const artifact = query.body.data.artifacts.find(item => item.role === 'output' && /pdb|mmcif|cif/.test(item.media_type));
          assert(artifact?.download.available && artifact.download.href.startsWith('/admin/'), 'authorized structure download required');
          const link = page.locator('a').filter({hasText: 'Download artifact'}).and(page.locator(`a[href="${artifact.download.href}"]`));
          const started = performance.now();
          const promise = page.waitForEvent('download');
          await link.click();
          const download = await promise;
          assert.equal(await download.failure(), null);
          const bytes = fs.readFileSync(await download.path());
          const digest = crypto.createHash('sha256').update(bytes).digest('hex');
          assert.equal('sha256:' + digest, artifact.sha256);
          assert.equal(bytes.length, artifact.size_bytes.value);
          report.downloads.push({operation_id: id, artifact_id: artifact.artifact_id, bytes: bytes.length,
            sha256: digest, seconds: (performance.now() - started) / 1000, at: new Date().toISOString(), status: 'passed'});
          await download.delete();
        } else assert.equal(command.action, 'snapshot', 'unsupported browser command');
        const record = {at: new Date().toISOString(), action: command, url: page.url(),
          snapshot: await page.locator('body').ariaSnapshot(), observed_queries: report.observed_queries};
        save(command.label + '.json', record);
        await page.screenshot({path: path.join(output, 'output/playwright', command.label + '.png'), fullPage: true});
        report.actions.push({at: record.at, command, status: 'passed', observed_queries: report.observed_queries});
        save('session-report.json', report);
        console.log(JSON.stringify(record));
      } catch (error) {
        const failure = {at: new Date().toISOString(), command, status: 'failed', message: redacted(error.message)};
        report.actions.push(failure);
        save('session-report.json', report);
        console.log(JSON.stringify(failure));
      }
    }
    input.close();
    process.stdin.pause();
  } finally {
    await browser.close();
    await Promise.allSettled([...pendingResponses]);
    report.browser_closed_at = new Date().toISOString();
    save('session-report.json', report);
    console.log(JSON.stringify({status: 'closed', at: report.browser_closed_at, observed_queries: report.observed_queries}));
  }
}
main().catch(error => {console.error(JSON.stringify({status: 'harness-failed', type: error.name})); process.exitCode = 1;});
