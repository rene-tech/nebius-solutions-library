const { test } = require("node:test");
const assert = require("node:assert/strict");
const { qwenFixture, runKeyInference } = require("./key_inference.cjs");

function fakeRequest(responses) {
  const calls = [];
  return {
    calls,
    async fetch(url, options) {
      calls.push({ url, options });
      const next = responses.shift();
      if (next instanceof Error) throw next;
      assert(next, "no hidden retry or extra request allowed");
      return {
        status: () => next.status,
        ok: () => next.status >= 200 && next.status < 300,
        body: async () => Buffer.from(JSON.stringify(next.body)),
        headers: () => next.headers ?? {},
      };
    },
  };
}
const operationId = "11111111-2222-3333-4444-555555555555";
const settings = {
  origin: "https://test.invalid",
  secret: "test-key-must-remain-private",
  keyId: "key-id",
  modelId: "app-qwen-clone",
  sleep: async () => undefined,
};

test("the unchanged arithmetic fixture changes only the independently routed model", () => {
  assert.deepEqual(qwenFixture(settings.modelId), {
    model: "app-qwen-clone",
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
  });
});

test("one submission plus a status read and a real exact semantic result, without secret evidence", async () => {
  const request = fakeRequest([
    { status: 202, body: { id: operationId, status: "running" } },
    {
      status: 200,
      body: {
        id: operationId,
        status: "succeeded",
        model_id: settings.modelId,
      },
    },
    {
      status: 200,
      body: {
        choices: [{ message: { content: "42" } }],
        usage: { completion_tokens: 1 },
      },
    },
  ]);
  const result = await runKeyInference(request, settings);
  assert.equal(result.status, "passed");
  assert.equal(result.submission_attempts, 1);
  assert.equal(result.status_polls, 1);
  assert.equal(result.actual_output, "42");
  assert.equal(
    request.calls.filter((call) => call.options.method === "POST").length,
    1,
  );
  assert(request.calls.every((call) => call.options.maxRetries === 0));
  assert.equal(
    request.calls[0].options.headers.authorization,
    `Bearer ${settings.secret}`,
  );
  assert(!JSON.stringify(result).includes(settings.secret));
});

test("a failed admission is retained and never retried", async () => {
  const request = fakeRequest([{ status: 409, body: { detail: "conflict" } }]);
  const result = await runKeyInference(request, settings);
  assert.equal(result.status, "failed");
  assert.equal(result.exchanges[0].http_status, 409);
  assert.deepEqual(result.exchanges[0].error, { detail: "conflict" });
  assert.equal(result.submission_attempts, 1);
  assert.equal(request.calls.length, 1);
});

test("failure receipts retain bounded backend reason without credentials or headers", async () => {
  const request = fakeRequest([
    {
      status: 404,
      body: {
        error: {
          code: "model_not_found",
          message: `route absent ${settings.secret}`,
        },
        authorization: settings.secret,
        arbitrary_response_field: "not collected",
      },
    },
  ]);
  const result = await runKeyInference(request, settings);
  assert.deepEqual(result.exchanges[0].error, {
    error: { code: "model_not_found", message: "route absent [redacted]" },
  });
  assert.match(result.exchanges[0].response_sha256, /^[a-f0-9]{64}$/);
  assert(!JSON.stringify(result).includes(settings.secret));
  assert.equal(request.calls.length, 1);
});

test("successful transport with wrong semantic output still fails", async () => {
  const request = fakeRequest([
    {
      status: 202,
      body: {
        id: operationId,
        status: "succeeded",
        model_id: settings.modelId,
      },
    },
    { status: 200, body: { choices: [{ message: { content: "43" } }] } },
  ]);
  const result = await runKeyInference(request, settings);
  assert.equal(result.status, "failed");
  assert.equal(result.actual_output, "43");
  assert.equal(result.correctness_passed, false);
});
