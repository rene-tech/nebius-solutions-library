// Real-browser observation only. No storageState or credentials are written.
// CLI-first bootstrap was unavailable because its VM forbids local file reads;
// this uses the existing repo's Playwright module with in-memory credentials.
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');
const { chromium } = require('/home/tux/.npm/_npx/e41f203b7505f1fb/node_modules/playwright');

async function main() {
  process.umask(0o077);
  const [credentialPath, output] = process.argv.slice(2);
  const credentials = JSON.parse(fs.readFileSync(credentialPath)).credentials;
  fs.mkdirSync(output, {recursive: true, mode: 0o700});
  fs.mkdirSync(path.join(output, 'output/playwright'), {recursive: true, mode: 0o700});
  const browser = await chromium.launch({headless: true, executablePath: '/usr/bin/google-chrome'});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  const errors = [];
  page.on('pageerror', error => errors.push({at: new Date().toISOString(), name: error.name}));
  page.on('response', response => {
    if (response.status() >= 400) errors.push({at: new Date().toISOString(), status: response.status(), path: new URL(response.url()).pathname});
  });
  await page.goto('https://89.169.99.188/admin/scientific-runs');
  await page.getByRole('textbox', {name: 'Bootstrap access token'}).fill(credentials.admin_bootstrap_token);
  await page.getByRole('button', {name: 'Sign in', exact: true}).click();
  await page.getByRole('navigation').first().waitFor();
  console.log(JSON.stringify({status: 'signed-in', role: 'bootstrap-admin operator, not customer self-service', url: page.url()}));
  const input = readline.createInterface({input: process.stdin});
  for await (const line of input) {
    let command;
    try {
      command = JSON.parse(line);
      if (command.action === 'close') break;
      const started = Date.now();
      if (command.action === 'navigate') {
        if (!command.path.startsWith('/admin/')) throw new Error('Only admin read-only navigation');
        await page.goto('https://89.169.99.188' + command.path);
        await page.waitForLoadState('networkidle');
      } else if (command.action === 'click-link') {
        await page.getByRole('link', {name: command.name, exact: true}).click();
        await page.waitForLoadState('networkidle');
      } else if (command.action === 'search') {
        await page.getByRole('searchbox', {name: command.name, exact: true}).fill(command.value);
        await page.waitForLoadState('networkidle');
      } else if (command.action !== 'snapshot') throw new Error('Unsupported read-only browser action');
      const snapshot = await page.locator('body').ariaSnapshot();
      const record = {at: new Date().toISOString(), action: command, url: page.url(),
                      browser_action_seconds: (Date.now() - started) / 1000, snapshot, errors: [...errors]};
      for (const value of Object.values(credentials)) if (typeof value === 'string' && snapshot.includes(value)) throw new Error('Credential found in page snapshot');
      const name = command.label || String(Date.now());
      fs.writeFileSync(path.join(output, name + '.json'), JSON.stringify(record, null, 2) + '\n', {mode: 0o600});
      await page.screenshot({path: path.join(output, 'output/playwright', name + '.png'), fullPage: true});
      console.log(JSON.stringify(record));
    } catch (error) {
      console.log(JSON.stringify({status: 'observation-failed', at: new Date().toISOString(), command, failure_type: error.name, message: error.message}));
    }
  }
  await browser.close();
  console.log(JSON.stringify({status: 'closed', at: new Date().toISOString()}));
}

main().catch(error => { console.error(JSON.stringify({status: 'failed', failure_type: error.name})); process.exitCode = 1; });
