import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolveBindHost, DEFAULT_BIND_HOST } from "../src/bindHost.js";

test("default bind host is loopback only", () => {
  assert.equal(DEFAULT_BIND_HOST, "127.0.0.1");
  assert.equal(resolveBindHost({}), "127.0.0.1");
  assert.equal(resolveBindHost({ MOP_BIND_HOST: "  " }), "127.0.0.1");
});

test("MOP_BIND_HOST is an explicit override; MOP_HOST is ignored", () => {
  assert.equal(resolveBindHost({ MOP_BIND_HOST: "0.0.0.0" }), "0.0.0.0");
  assert.equal(resolveBindHost({ MOP_HOST: "http://localhost:3100" }), "127.0.0.1");
});

test("server.ts passes the resolved host to serve()", () => {
  const src = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
  assert.match(src, /serve\(\{ fetch: app\.fetch, port, hostname: bindHost \}/);
});
