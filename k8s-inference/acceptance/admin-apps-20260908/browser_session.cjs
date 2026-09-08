// Actual Apps acceptance session. No execution before root's exact release signal.
// Read-only by default; task-owned clone/user/key actions require --allow-mutations.
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const readline = require("node:readline");
const {
  verifyAppIdentity,
  verifyMetrics,
  verifyRunPublication,
} = require("./browser_checks.cjs");

const ORIGIN = "https://89.169.99.188";
const PREFIX = "/admin/api/v1";
const UUID = /^[a-f0-9-]{36}$/;
const TASK_NAME = /^trial-apps-20260908-[a-z0-9-]+$/;
const SECRET_KEYS =
  /^(secret|token|access_token|refresh_token|authorization|cookie|password|admin_bootstrap_token)$/i;

function sanitize(value, secrets) {
  if (typeof value === "string") {
    for (const secret of secrets)
      if (secret.length > 8) value = value.replaceAll(secret, "[redacted]");
    return value;
  }
  if (Array.isArray(value)) return value.map((item) => sanitize(item, secrets));
  if (value && typeof value === "object")
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        key,
        SECRET_KEYS.test(key) ? "[redacted]" : sanitize(item, secrets),
      ]),
    );
  return value;
}

function adminPath(value) {
  const url = new URL(value, ORIGIN);
  assert.equal(url.origin, ORIGIN);
  assert(
    /^\/admin\/(apps(?:\/[a-f0-9-]{36}(?:\/(?:runs(?:\/[a-f0-9-]{36})?|metrics|logs|containers|usage|settings))?)?|users(?:\/[a-f0-9-]{36})?|capacity)$/.test(
      url.pathname,
    ),
  );
  assert(
    [...url.searchParams.keys()].every((key) =>
      [
        "from",
        "to",
        "window",
        "status",
        "principal_id",
        "tenant_id",
        "search",
        "project",
        "cluster",
        "region",
      ].includes(key),
    ),
  );
  return url.href;
}

async function main() {
  process.umask(0o077);
  const [credentialPath, output, sourceCommit, ...flags] =
    process.argv.slice(2);
  const allowMutations = flags.includes("--allow-mutations");
  assert(flags.every((flag) => flag === "--allow-mutations"));
  assert(
    output && !fs.existsSync(output) && /^[a-f0-9]{40}$/.test(sourceCommit),
    "fresh output and exact source required",
  );
  assert.equal(
    fs.statSync(credentialPath).mode & 0o077,
    0,
    "credential bundle must be private",
  );
  const credentials = JSON.parse(
    fs.readFileSync(credentialPath, "utf8"),
  ).credentials;
  assert(typeof credentials.admin_bootstrap_token === "string");
  const secrets = new Set(
    Object.values(credentials).filter((value) => typeof value === "string"),
  );
  fs.mkdirSync(path.join(output, "output/playwright"), {
    recursive: true,
    mode: 0o700,
  });
  const report = {
    source_commit: sourceCommit,
    origin: ORIGIN,
    allow_mutations: allowMutations,
    started_at: new Date().toISOString(),
    browser_version: null,
    actions: [],
    errors: [],
    console: [],
    failed_requests: [],
    downloads: [],
    checks: [],
    cleanup: [],
    observed_queries: 0,
  };
  const queries = [];
  const pending = new Set();
  const clones = new Map();
  const sources = new Map();
  const users = new Set();
  const keys = new Map();
  const keyOwners = new Map();
  const revokedKeys = new Set();
  function save(name, value) {
    fs.writeFileSync(
      path.join(output, name),
      JSON.stringify(sanitize(value, secrets), null, 2) + "\n",
      { mode: 0o600 },
    );
  }
  function emit(value) {
    console.log(JSON.stringify(sanitize(value, secrets)));
  }
  function rememberDisclosure(body) {
    if (typeof body?.data?.secret === "string") {
      secrets.add(body.data.secret);
      if (UUID.test(body.data.key?.id ?? ""))
        keys.set(body.data.key.id, body.data.secret);
    }
  }
  const { chromium } = require(
    process.env.FS2_PLAYWRIGHT_MODULE ||
      "/home/tux/.npm/_npx/e41f203b7505f1fb/node_modules/playwright",
  );
  const browser = await chromium.launch({
    headless: true,
    executablePath: "/usr/bin/google-chrome",
  });
  report.browser_version = browser.version();
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    locale: "en-US",
    timezoneId: "UTC",
    acceptDownloads: true,
  });
  const page = await context.newPage();
  page.setDefaultTimeout(30000);
  page.on("pageerror", (error) =>
    report.errors.push({
      at: new Date().toISOString(),
      kind: "pageerror",
      message: error.message,
    }),
  );
  page.on("console", (item) => {
    if (["error", "warning"].includes(item.type()))
      report.console.push({
        at: new Date().toISOString(),
        type: item.type(),
        message: item.text(),
      });
  });
  page.on("requestfailed", (request) =>
    report.failed_requests.push({
      at: new Date().toISOString(),
      path: new URL(request.url()).pathname,
      failure: request.failure(),
    }),
  );
  page.on("response", (response) => {
    const url = new URL(response.url());
    if (response.status() >= 400)
      report.errors.push({
        at: new Date().toISOString(),
        path: url.pathname,
        status: response.status(),
      });
    if (
      !url.pathname.startsWith(PREFIX) ||
      url.pathname.includes("/sessions") ||
      url.pathname.endsWith("/content")
    )
      return;
    const ordinal = ++report.observed_queries;
    const entry = {
      at: new Date().toISOString(),
      path: url.pathname,
      query: url.search,
      method: response.request().method(),
      status: response.status(),
    };
    const work = (async () => {
      const body = await response.json();
      rememberDisclosure(body);
      entry.body = sanitize(body, secrets);
      queries.push(entry);
      save(`query-${String(ordinal).padStart(5, "0")}.json`, entry);
    })().catch((error) =>
      report.errors.push({
        at: new Date().toISOString(),
        kind: "response-capture",
        path: url.pathname,
        message: error.message,
      }),
    );
    pending.add(work);
    work.finally(() => pending.delete(work));
  });
  async function api(method, suffix, data) {
    const response = await context.request.fetch(ORIGIN + PREFIX + suffix, {
      method,
      data,
      headers: { origin: ORIGIN },
      maxRetries: 0,
    });
    const body = await response.json();
    assert(
      response.ok(),
      `authorized ${method} ${suffix} returned ${response.status()}`,
    );
    return body.data;
  }
  async function screenshot(label) {
    // Never snapshot a one-time secret, including failure before its handler runs.
    const disclosure = await page
      .getByRole("dialog", { name: "API key created", exact: true })
      .isVisible();
    const record = {
      at: new Date().toISOString(),
      url: page.url(),
      observed_queries: report.observed_queries,
      snapshot: disclosure
        ? "One-time credential dialog deliberately omitted"
        : await page.locator("body").ariaSnapshot(),
    };
    save(label + ".json", record);
    await page.screenshot({
      path: path.join(output, "output/playwright", label + ".png"),
      fullPage: true,
      mask: [
        page.locator(".secret-field"),
        page.getByRole("textbox", { name: "Bootstrap access token" }),
      ],
    });
    return record;
  }
  async function fields(items) {
    for (const item of items) {
      assert(
        typeof item.label === "string" &&
          !/secret|token|password/i.test(item.label),
      );
      const locator = page.getByLabel(item.label, { exact: true });
      if (item.kind === "select")
        await locator.selectOption(String(item.value));
      else if (item.kind === "check")
        await locator.setChecked(Boolean(item.value));
      else await locator.fill(String(item.value));
    }
  }
  async function mutationResponse(method, suffix, action) {
    const responsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === method &&
        new URL(response.url()).pathname === PREFIX + suffix,
    );
    await action();
    const response = await responsePromise;
    const body = await response.json();
    rememberDisclosure(body);
    assert(
      response.ok(),
      `browser ${method} ${suffix} returned ${response.status()}`,
    );
    return body.data;
  }
  try {
    await page.goto(ORIGIN + "/admin/apps");
    await page
      .getByRole("textbox", { name: "Bootstrap access token" })
      .fill(credentials.admin_bootstrap_token);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await page.getByRole("navigation", { name: "Main", exact: true }).waitFor();
    await page
      .getByRole("button", { name: "Create app", exact: true })
      .waitFor();
    await page.getByRole("table", { name: "Apps and logical usage" }).waitFor();
    report.signed_in_at = new Date().toISOString();
    await screenshot("initial-ready");
    save("session-report.json", report);
    emit({
      status: "signed-in-ready",
      at: report.signed_in_at,
      source_commit: sourceCommit,
      browser: report.browser_version,
    });
    const input = readline.createInterface({ input: process.stdin });
    for await (const line of input) {
      let command;
      try {
        command = JSON.parse(line);
        if (command.action === "close") break;
        assert(
          /^[a-zA-Z0-9_-]+$/.test(command.label) &&
            !fs.existsSync(path.join(output, command.label + ".json")),
          "fresh evidence label required",
        );
        if (command.action === "navigate") {
          await page.goto(adminPath(command.path));
          await page
            .getByRole("navigation", { name: "Main", exact: true })
            .waitFor();
        } else if (command.action === "tab") {
          assert(
            [
              "Runs",
              "Metrics",
              "App Logs",
              "Containers",
              "Usage",
              "Settings",
            ].includes(command.name),
          );
          await page
            .getByRole("navigation", { name: "App sections" })
            .getByRole("link", { name: command.name, exact: true })
            .click();
        } else if (command.action === "range") {
          assert(["1h", "6h", "24h", "7d"].includes(command.value));
          await page
            .getByLabel("Time range", { exact: true })
            .selectOption(command.value);
        } else if (command.action === "filter") {
          assert(
            [
              "Search apps",
              "Run state",
              "Run user",
              "Search logs",
              "Pod",
              "Container",
              "Search users",
            ].includes(command.field),
          );
          await fields([
            { label: command.field, value: command.value, kind: command.kind },
          ]);
        } else if (command.action === "verify-metrics") {
          await Promise.allSettled([...pending]);
          const row = queries.findLast(
            (item) =>
              item.path === `${PREFIX}/apps/${command.app_id}/metrics` &&
              item.status === 200,
          );
          assert(row, "actual metric response required");
          report.checks.push({
            kind: "metrics",
            ...verifyMetrics(row.body.data, {
              requireNonzero: command.require_nonzero === true,
            }),
          });
        } else if (command.action === "verify-publication") {
          await Promise.allSettled([...pending]);
          report.checks.push({
            kind: "publication",
            ...verifyRunPublication(queries, command.operation_id),
          });
          await page
            .getByText("Publication complete · polling stopped", {
              exact: false,
            })
            .waitFor();
        } else if (command.action === "download") {
          await Promise.allSettled([...pending]);
          const row = queries.findLast(
            (item) =>
              item.body?.data?.operation?.id === command.operation_id &&
              item.status === 200,
          );
          const artifact = row?.body.data.scientific?.artifacts.find(
            (item) =>
              item.role === "output" &&
              item.download.available &&
              (!command.artifact_id ||
                item.artifact_id === command.artifact_id),
          );
          assert(
            artifact?.download.href?.startsWith(PREFIX + "/scientific-runs/"),
          );
          const href = artifact.download.href;
          const link = page
            .locator("a")
            .filter({ hasText: "Download" })
            .and(page.locator(`a[href="${href}"]`));
          const started = performance.now();
          const downloadPromise = page.waitForEvent("download");
          await link.click();
          const download = await downloadPromise;
          assert.equal(await download.failure(), null);
          const bytes = fs.readFileSync(await download.path());
          const digest = crypto
            .createHash("sha256")
            .update(bytes)
            .digest("hex");
          assert.equal(digest, artifact.sha256.replace(/^sha256:/, ""));
          assert.equal(bytes.length, artifact.size_bytes.value);
          report.downloads.push({
            operation_id: command.operation_id,
            artifact_id: artifact.artifact_id,
            bytes: bytes.length,
            sha256: digest,
            seconds: (performance.now() - started) / 1000,
            at: new Date().toISOString(),
          });
          await download.delete();
        } else if (command.action === "create-app") {
          assert(
            allowMutations &&
              UUID.test(command.source_app_id) &&
              TASK_NAME.test(command.name),
          );
          const source = await api("GET", `/apps/${command.source_app_id}`);
          sources.set(
            source.app_id,
            await api("GET", `/apps/${source.app_id}/settings`),
          );
          await page.goto(adminPath("/admin/apps"));
          await page
            .getByRole("button", { name: "Create app", exact: true })
            .click();
          await fields([
            { label: "App name", value: command.name },
            { label: "Base deployment", value: source.app_id, kind: "select" },
          ]);
          const clone = await mutationResponse("POST", "/apps", () =>
            page
              .getByRole("button", {
                name: "Create independent app",
                exact: true,
              })
              .click(),
          );
          clones.set(clone.app_id, null); // Track committed identity before the next read can fail.
          clones.set(
            clone.app_id,
            await api("GET", `/apps/${clone.app_id}/settings`),
          );
          report.checks.push({
            kind: "clone",
            ...verifyAppIdentity(source, clone),
          });
        } else if (command.action === "set-workers") {
          assert(
            allowMutations && clones.has(command.app_id),
            "only this session's task-owned clone can change",
          );
          assert(
            Number.isInteger(command.minimum) &&
              Number.isInteger(command.maximum) &&
              command.minimum >= 0 &&
              command.maximum <= 2 &&
              command.minimum <= command.maximum,
          );
          await page.goto(adminPath(`/admin/apps/${command.app_id}/settings`));
          await fields([
            { label: "Minimum ready workers", value: command.minimum },
            { label: "Maximum workers", value: command.maximum },
          ]);
          const updated = await mutationResponse(
            "PATCH",
            `/apps/${command.app_id}/settings`,
            () =>
              page
                .getByRole("button", { name: "Save settings", exact: true })
                .click(),
          );
          assert.equal(
            updated.serving.spec.availability.minReplicas,
            command.minimum,
          );
          assert.equal(
            updated.serving.spec.availability.maxReplicas,
            command.maximum,
          );
          clones.set(command.app_id, updated);
          report.checks.push({
            kind: "runtime-settings-saved",
            app_id: command.app_id,
            app_revision: updated.app_revision,
            desired_revision: updated.serving.revision,
            observed_ready:
              "must be verified separately from actual Containers/status response",
          });
        } else if (command.action === "create-user") {
          assert(
            allowMutations &&
              TASK_NAME.test(command.name) &&
              TASK_NAME.test(command.principal_id),
          );
          await page.goto(adminPath("/admin/users"));
          await page
            .getByRole("button", { name: "Create user", exact: true })
            .click();
          await fields([
            { label: "Display name", value: command.name },
            { label: "Owner identity", value: command.principal_id },
            { label: "Tenant", value: command.tenant_id },
          ]);
          const user = await mutationResponse("POST", "/users", () =>
            page
              .getByRole("button", { name: "Save user", exact: true })
              .click(),
          );
          users.add(user.id);
          report.checks.push({
            kind: "user-created",
            user_id: user.id,
            principal_id: user.principal_id,
            tenant_id: user.tenant_id,
          });
        } else if (command.action === "create-key") {
          assert(
            allowMutations &&
              users.has(command.user_id) &&
              TASK_NAME.test(command.name),
          );
          assert(
            typeof command.model_id === "string" && command.model_id !== "*",
          );
          await page.goto(adminPath(`/admin/users/${command.user_id}`));
          await page
            .getByRole("button", { name: "Create API key", exact: true })
            .click();
          await fields([
            { label: "Key name", value: command.name },
            { label: "Allowed models", value: command.model_id },
          ]);
          const scopeFields = page
            .getByRole("dialog", { name: "Create API key", exact: true })
            .getByRole("checkbox");
          for (const field of await scopeFields.all()) await field.uncheck();
          for (const scope of [
            "catalog.read",
            "inference.invoke",
            "operations.read",
            "operations.result",
          ])
            await page.getByLabel(scope, { exact: true }).check();
          const disclosure = await mutationResponse(
            "POST",
            `/users/${command.user_id}/keys`,
            () =>
              page
                .getByRole("button", { name: "Create key", exact: true })
                .click(),
          );
          keys.set(disclosure.key.id, disclosure.secret);
          keyOwners.set(disclosure.key.id, {
            user_id: command.user_id,
            name: command.name,
          });
          secrets.add(disclosure.secret);
          await page
            .getByRole("button", { name: "I have stored it", exact: true })
            .click();
          report.checks.push({
            kind: "key-created",
            key_id: disclosure.key.id,
            user_id: command.user_id,
            model_id: command.model_id,
          });
        } else if (command.action === "use-key-discovery") {
          assert(allowMutations && keys.has(command.key_id));
          const response = await context.request.get(ORIGIN + "/v1/models", {
            headers: { authorization: `Bearer ${keys.get(command.key_id)}` },
            maxRetries: 0,
          });
          assert.equal(response.status(), command.expected_status ?? 200);
          report.checks.push({
            kind: "key-discovery",
            key_id: command.key_id,
            http_status: response.status(),
            inference_submitted: false,
          });
        } else if (command.action === "revoke-key") {
          assert(
            allowMutations &&
              keys.has(command.key_id) &&
              keyOwners.has(command.key_id),
          );
          const owner = keyOwners.get(command.key_id);
          await page.goto(adminPath(`/admin/users/${owner.user_id}`));
          await page
            .getByRole("row")
            .filter({ hasText: owner.name })
            .getByRole("button", { name: "Revoke", exact: true })
            .click();
          await mutationResponse("DELETE", `/keys/${command.key_id}`, () =>
            page
              .getByRole("button", { name: "Revoke key", exact: true })
              .click(),
          );
          revokedKeys.add(command.key_id);
          report.checks.push({
            kind: "key-revoked",
            key_id: command.key_id,
            mechanism: "real Users UI confirmation",
          });
        } else
          assert.equal(
            command.action,
            "snapshot",
            "unsupported browser command",
          );
        const record = await screenshot(command.label);
        report.actions.push({
          at: record.at,
          command,
          status: "passed",
          observed_queries: report.observed_queries,
        });
        save("session-report.json", report);
        emit({
          status: "passed",
          action: command.action,
          label: command.label,
          at: record.at,
          url: record.url,
        });
      } catch (error) {
        report.actions.push({
          at: new Date().toISOString(),
          command,
          status: "failed",
          message: error.message,
        });
        save("session-report.json", report);
        emit({
          status: "failed",
          action: command?.action,
          label: command?.label,
          type: error.name,
          message: error.message,
        });
      }
    }
    input.close();
    process.stdin.pause();
  } finally {
    // Only resources created by this invocation; no source app settings are mutated.
    for (const [keyId] of keys) {
      if (revokedKeys.has(keyId)) continue;
      try {
        await api("DELETE", `/keys/${keyId}`);
        report.cleanup.push({ kind: "key-revoked", key_id: keyId });
      } catch (error) {
        report.cleanup.push({
          kind: "key-revoke-failed",
          key_id: keyId,
          error: error.message,
        });
      }
    }
    for (const userId of users) {
      try {
        await api("PATCH", `/users/${userId}`, { enabled: false });
        report.cleanup.push({ kind: "user-disabled", user_id: userId });
      } catch (error) {
        report.cleanup.push({
          kind: "user-disable-failed",
          user_id: userId,
          error: error.message,
        });
      }
    }
    for (const [appId, last] of clones) {
      try {
        const current = await api("GET", `/apps/${appId}/settings`);
        if (last) {
          assert.equal(
            current.app_revision,
            last.app_revision,
            "concurrent app revision requires root coordination",
          );
          assert.equal(
            current.serving.etag,
            last.serving.etag,
            "concurrent desired spec requires root coordination",
          );
        }
        const spec = structuredClone(current.serving.spec);
        spec.lifecycle.desiredState = "Disabled";
        const updated = await api("PATCH", `/apps/${appId}/settings`, {
          expected_app_revision: current.app_revision,
          serving_base_etag: current.serving.etag,
          serving_spec: spec,
        });
        assert.equal(updated.serving.spec.lifecycle.desiredState, "Disabled");
        report.cleanup.push({
          kind: "clone-disabled",
          app_id: appId,
          app_revision: updated.app_revision,
          resource_release:
            "requires subsequent actual container/controller observation",
        });
      } catch (error) {
        report.cleanup.push({
          kind: "clone-disable-failed",
          app_id: appId,
          error: error.message,
        });
      }
    }
    for (const [appId, original] of sources) {
      try {
        const current = await api("GET", `/apps/${appId}/settings`);
        assert.deepEqual(
          current,
          original,
          "source settings must remain exactly unchanged",
        );
        report.cleanup.push({
          kind: "source-settings-unchanged",
          app_id: appId,
        });
      } catch (error) {
        report.cleanup.push({
          kind: "source-comparison-failed",
          app_id: appId,
          error: error.message,
        });
      }
    }
    await browser.close();
    await Promise.allSettled([...pending]);
    report.browser_closed_at = new Date().toISOString();
    save("session-report.json", report);
    emit({
      status: "closed",
      at: report.browser_closed_at,
      queries: report.observed_queries,
      cleanup: report.cleanup,
    });
  }
}

if (require.main === module)
  main().catch((error) => {
    console.error(
      JSON.stringify({ status: "harness-failed", type: error.name }),
    );
    process.exitCode = 1;
  });
module.exports = { sanitize, adminPath };
