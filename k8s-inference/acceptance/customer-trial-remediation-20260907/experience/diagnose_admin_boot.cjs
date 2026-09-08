// Bounded unauthenticated page initialization diagnostic; no credentials or model calls.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require('/home/tux/.npm/_npx/e41f203b7505f1fb/node_modules/playwright');

async function main() {
  process.umask(0o077);
  const output = process.argv[2];
  assert(output && !fs.existsSync(output), 'new private output directory required');
  fs.mkdirSync(path.join(output, 'output/playwright'), {recursive: true, mode: 0o700});
  const report = {started_at: new Date().toISOString(), console: [], page_errors: [], requests: [], failed_requests: []};
  const browser = await chromium.launch({headless: true, executablePath: '/usr/bin/google-chrome'});
  try {
    const context = await browser.newContext({locale: 'en-US', timezoneId: 'UTC'});
    const page = await context.newPage();
    page.on('console', item => report.console.push({at: new Date().toISOString(), type: item.type(), text: item.text()}));
    page.on('pageerror', error => report.page_errors.push({at: new Date().toISOString(), name: error.name, message: error.message}));
    page.on('response', response => report.requests.push({at: new Date().toISOString(), url: response.url(),
      status: response.status(), type: response.request().resourceType(), headers: response.headers()}));
    page.on('requestfailed', request => report.failed_requests.push({at: new Date().toISOString(), url: request.url(),
      type: request.resourceType(), failure: request.failure()}));
    await page.goto('https://89.169.99.188/admin/scientific-runs');
    try {
      await page.getByRole('textbox', {name: 'Bootstrap access token'}).waitFor({timeout: 15000});
      report.sign_in_visible = true;
    } catch { report.sign_in_visible = false; }
    report.snapshot = await page.locator('body').ariaSnapshot();
    report.dom = await page.evaluate(() => ({readyState: document.readyState, body: document.body.outerHTML,
      scripts: Array.from(document.scripts).map(script => ({src: script.src, type: script.type})),
      resources: performance.getEntriesByType('resource').map(item => ({name: item.name, duration: item.duration,
        transferSize: item.transferSize, decodedBodySize: item.decodedBodySize}))}));
    await page.screenshot({path: path.join(output, 'output/playwright/boot.png'), fullPage: true});
  } finally {
    await browser.close();
    report.browser_closed_at = new Date().toISOString();
    fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n', {mode: 0o600});
    console.log(JSON.stringify(report));
  }
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
