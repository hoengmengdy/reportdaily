const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const assert = require("node:assert/strict");

const source = readFileSync(join(__dirname, "..", "app.js"), "utf8");
const start = source.indexOf("async function readApiResponse(");
const end = source.indexOf("async function api(", start);
assert.ok(start >= 0 && end > start);
const context = vm.createContext({});
vm.runInContext(source.slice(start, end), context);

test("accepts valid successful JSON", async () => {
  const result = await context.readApiResponse(Response.json({ pages: [], total_target: 82 }));
  assert.equal(result.total_target, 82);
});

test("preserves JSON error details for API error handling", async () => {
  const result = await context.readApiResponse(Response.json({ error: "Please sign in." }, { status: 401 }));
  assert.equal(result.error, "Please sign in.");
});

test("explains missing backend for Vercel's plain text 404", async () => {
  const response = new Response("The page could not be found\nNOT_FOUND", { status: 404 });
  await assert.rejects(context.readApiResponse(response), /API is missing.*404.*Flask/);
});

test("explains HTML proxy errors without exposing the response body", async () => {
  const response = new Response("<html>private gateway details</html>", { status: 502, headers: { "Content-Type": "text/html" } });
  await assert.rejects(context.readApiResponse(response), (error) => {
    assert.match(error.message, /HTTP 502/);
    assert.doesNotMatch(error.message, /private gateway details/);
    return true;
  });
});

test("rejects invalid JSON with a readable deployment message", async () => {
  const response = new Response("not-json", { headers: { "Content-Type": "application/json" } });
  await assert.rejects(context.readApiResponse(response), /API returned invalid JSON/);
});

test("rejects JSON values that are not API response objects", async () => {
  for (const value of [null, [], "a page", 42]) {
    await assert.rejects(context.readApiResponse(Response.json(value)), /invalid JSON/);
  }
});
