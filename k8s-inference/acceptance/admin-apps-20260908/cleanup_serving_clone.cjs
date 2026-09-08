// Explicit repair of this acceptance run's failed cleanup; never creates resources.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  request,
} = require("/home/tux/.npm/_npx/e41f203b7505f1fb/node_modules/playwright");
const { disabledCloneSpec } = require("./browser_checks.cjs");

const APP = "96c9e1e5-0d82-4532-a2e0-0218cd2a99e0";
const SOURCE = "63e4fa62-39f9-58b1-a4cc-b7e73df8fbf2";
const ROUTE = "app-96c9e1e50d824532a2e00218cd2a99e0";
const ORIGIN = "https://89.169.99.188";

async function main() {
  process.umask(0o077);
  const [credentialPath, output, execute] = process.argv.slice(2);
  assert.equal(execute, "--execute");
  assert(output && !fs.existsSync(output), "fresh evidence path required");
  fs.mkdirSync(path.dirname(output), { recursive: true, mode: 0o700 });
  const access = JSON.parse(fs.readFileSync(credentialPath));
  const context = await request.newContext({
    baseURL: ORIGIN,
    extraHTTPHeaders: { origin: ORIGIN },
    timeout: 30000,
  });
  const report = {
    app_id: APP,
    started_at: new Date().toISOString(),
    exchanges: [],
  };
  const save = () =>
    fs.writeFileSync(output, JSON.stringify(report, null, 2) + "\n");
  async function api(method, suffix, data) {
    const response = await context.fetch(`/admin/api/v1${suffix}`, {
      method,
      data,
      maxRetries: 0,
    });
    const body = await response.json();
    report.exchanges.push({
      at: new Date().toISOString(),
      method,
      suffix,
      status: response.status(),
      body,
    });
    save();
    assert(response.ok(), `${method} ${suffix}: ${response.status()}`);
    return body.data;
  }
  try {
    const login = await context.post("/admin/api/v1/session", {
      headers: {
        authorization: `Bearer ${access.credentials.admin_bootstrap_token}`,
      },
      maxRetries: 0,
    });
    assert(login.ok(), `login ${login.status()}`);
    const beforeSource = await api("GET", `/apps/${SOURCE}/settings`);
    const current = await api("GET", `/apps/${APP}/settings`);
    assert.equal(current.display_name, "trial-apps-20260908-serving-r01");
    assert.equal(current.app_revision, 2, "unexpected concurrent app revision");
    assert.equal(current.serving.spec.modelRef, "qwen3-8b");
    assert.equal(current.serving.spec.app.appId, APP);
    assert.equal(current.serving.spec.app.publicModelId, ROUTE);
    assert.equal(
      current.serving.etag,
      "sha256:399d76ef0ba7e235e079c62cedacca7f7ff6c374492be1a2c6486a5e6c9e12c8",
    );
    const updated = await api("PATCH", `/apps/${APP}/settings`, {
      expected_app_revision: current.app_revision,
      serving_base_etag: current.serving.etag,
      serving_spec: disabledCloneSpec(current.serving.spec),
    });
    assert.equal(updated.serving.spec.lifecycle.desiredState, "Disabled");
    assert.equal(updated.serving.spec.availability.minReplicas, 0);
    assert.deepEqual(updated.serving.spec.availability.warmWindows, []);
    report.desired_revision = updated.serving.revision;
    const deadline = Date.now() + 300000;
    while (Date.now() < deadline) {
      const status = await api("GET", `/model-deployments/${ROUTE}/status`);
      const containers = await api("GET", `/apps/${APP}/containers`);
      const observed = status.observation?.status;
      const replicas = observed?.replicas;
      if (
        replicas?.desired === 0 &&
        replicas.ready === 0 &&
        replicas.available === 0 &&
        observed.spec_digest === updated.serving.etag &&
        containers.state === "available" &&
        containers.items.length === 0
      ) {
        report.zero_workers_observed_at = new Date().toISOString();
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 5000));
    }
    assert(
      report.zero_workers_observed_at,
      "zero desired/actual workers not observed within five minutes",
    );
    assert.deepEqual(
      await api("GET", `/apps/${SOURCE}/settings`),
      beforeSource,
    );
    report.source_settings_unchanged = true;
    report.status = "passed";
  } catch (error) {
    report.status = "failed";
    report.error = error.message;
    process.exitCode = 1;
  } finally {
    const logout = await context.delete("/admin/api/v1/session", {
      maxRetries: 0,
    });
    report.logout_status = logout.status();
    await context.dispose();
    report.finished_at = new Date().toISOString();
    save();
    console.log(
      JSON.stringify({
        status: report.status,
        app_id: APP,
        desired_revision: report.desired_revision,
        zero_workers_observed_at: report.zero_workers_observed_at,
        error: report.error,
        output,
      }),
    );
  }
}
main().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
