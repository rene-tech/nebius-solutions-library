// One actual inference with a newly issued scoped key. No submission retries.
// The fixture matches the retained customer-trial arithmetic probe; only the
// public model ID changes to the independently created serving app route.
const assert = require("node:assert/strict");
const crypto = require("node:crypto");

function qwenFixture(modelId) {
  return {
    model: modelId,
    messages: [
      {
        role: "user",
        content: "What is 17 plus 25? Reply with only the integer.",
      },
    ],
    max_tokens: 32,
    temperature: 0,
    stream: false,
    chat_template_kwargs: { enable_thinking: false },
  };
}

function safeErrorFields(value, secret) {
  if (typeof value === "string")
    return value.replaceAll(secret, "[redacted]").slice(0, 1024);
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return Object.fromEntries(
    ["error", "detail", "code", "type", "message"]
      .filter((key) => Object.hasOwn(value, key))
      .map((key) => [key, safeErrorFields(value[key], secret)]),
  );
}

async function runKeyInference(
  request,
  {
    origin,
    secret,
    keyId,
    modelId,
    sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  },
) {
  const payload = qwenFixture(modelId);
  const record = {
    key_id: keyId,
    model_id: modelId,
    request: payload,
    request_sha256: crypto
      .createHash("sha256")
      .update(JSON.stringify(payload))
      .digest("hex"),
    expected_output: "42",
    submission_attempts: 0,
    status_polls: 0,
    exchanges: [],
    started_at: new Date().toISOString(),
    status: "failed",
    correctness_passed: false,
  };
  const started = performance.now();
  async function exchange(method, pathname, data, extraHeaders = {}) {
    const start = performance.now();
    const row = {
      method,
      path: pathname,
      started_at: new Date().toISOString(),
      request_bytes:
        data === undefined ? 0 : Buffer.byteLength(JSON.stringify(data)),
    };
    record.exchanges.push(row);
    const response = await request.fetch(origin + pathname, {
      method,
      data,
      timeout: 30000,
      maxRetries: 0,
      headers: { authorization: `Bearer ${secret}`, ...extraHeaders },
    });
    const bytes = await response.body();
    Object.assign(row, {
      http_status: response.status(),
      response_bytes: bytes.length,
      response_sha256: crypto.createHash("sha256").update(bytes).digest("hex"),
      duration_seconds: (performance.now() - start) / 1000,
    });
    if (!response.ok()) {
      try {
        row.error = safeErrorFields(JSON.parse(bytes.toString("utf8")), secret);
      } catch {
        row.error = {
          detail: "Non-JSON error body; exact byte count/hash retained.",
        };
      }
    }
    assert(response.ok(), `HTTP ${response.status()}`);
    return {
      body: JSON.parse(bytes.toString("utf8")),
      headers: response.headers(),
    };
  }
  try {
    record.idempotency_key = "apps-key-acceptance-" + crypto.randomUUID();
    record.submission_attempts = 1;
    const admitted = await exchange("POST", "/v1/chat/completions", payload, {
      "Idempotency-Key": record.idempotency_key,
      "x-fs2-wait-seconds": "0",
      "x-fs2-deadline-seconds": "120",
    });
    const operationId =
      admitted.headers["x-fs2-operation-id"] || admitted.body.id;
    assert(
      /^[a-f0-9-]{36}$/.test(operationId),
      "durable operation ID required",
    );
    record.operation_id = operationId;
    let current = admitted.body;
    while (
      !["succeeded", "failed", "cancelled", "expired"].includes(current.status)
    ) {
      assert(
        performance.now() - started < 125000,
        "operation deadline exceeded",
      );
      await sleep(500);
      record.status_polls++;
      current = (await exchange("GET", `/v1/operations/${operationId}`)).body;
    }
    record.operation = current;
    assert.equal(
      current.status,
      "succeeded",
      "operation must actually succeed",
    );
    assert.equal(
      current.model_id,
      modelId,
      "operation must belong to the independent public route",
    );
    const result = (
      await exchange("GET", `/v1/operations/${operationId}/result`)
    ).body;
    const content = result.choices?.[0]?.message?.content;
    assert.equal(typeof content, "string", "actual model output required");
    record.actual_output = content.trim();
    record.response_sha256 = crypto
      .createHash("sha256")
      .update(JSON.stringify(result))
      .digest("hex");
    record.usage = result.usage ?? null;
    assert.equal(
      record.actual_output,
      record.expected_output,
      "semantic output mismatch",
    );
    record.correctness_passed = true;
    record.status = "passed";
  } catch (error) {
    // SDK errors may embed request headers; persist only the safe class.
    record.failure_type = error.name;
  }
  record.completed_at = new Date().toISOString();
  record.client_seconds = (performance.now() - started) / 1000;
  return record;
}

module.exports = { qwenFixture, runKeyInference };
