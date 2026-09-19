import assert from "node:assert/strict";
import { test } from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import { LiveInput } from "./live-input.js";

test("continuous typing starts promptly and preserves keys during a slow reply", async () => {
  const received = [],
    authority = {};
  let release;
  const queue = new LiveInput({
    delay: 2,
    async send(bytes) {
      received.push(new TextDecoder().decode(bytes));
      if (received.length === 1)
        await new Promise((resolve) => (release = resolve));
    },
    failed(error) {
      throw error;
    },
  });
  queue.push("a", authority);
  await delay(10);
  assert.deepEqual(received, ["a"]);
  for (const char of "bcdef") queue.push(char, authority);
  assert.deepEqual(received, ["a"]);
  release();
  await queue.flush();
  assert.equal(received.join(""), "abcdef");
  assert.equal(queue.active, false);
});

test("large Unicode paste uses valid bounded UTF-8 messages in exact order", async () => {
  const received = [];
  const queue = new LiveInput({
    send(bytes) {
      assert.ok(bytes.length <= 4096);
      received.push(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    },
    failed(error) {
      throw error;
    },
  });
  const text = "a🦊漢é".repeat(6000);
  queue.push(text, {});
  await queue.flush();
  assert.ok(received.length > 10);
  assert.equal(received.join(""), text);
});

test("uncertain delivery discards queued input and never retries", async () => {
  let release,
    attempts = 0,
    failures = 0;
  const queue = new LiveInput({
    async send() {
      attempts++;
      await new Promise((resolve) => (release = resolve));
      throw Error("Unknown outcome");
    },
    failed() {
      failures++;
    },
  });
  queue.push("first", {});
  const pending = queue.flush();
  queue.push("discard this", {});
  release();
  await pending;
  await delay(20);
  assert.equal(attempts, 1);
  assert.equal(failures, 1);
  assert.equal(queue.active, false);
});

test("switching panes cannot send old buffered keys or contaminate new input", async () => {
  let reject,
    errors = 0;
  const received = [];
  const queue = new LiveInput({
    async send(bytes, authority) {
      received.push([new TextDecoder().decode(bytes), authority]);
      if (authority === "old")
        await new Promise((_, failed) => (reject = failed));
    },
    failed() {
      errors++;
    },
  });
  queue.push("already sent", "old");
  const pending = queue.flush();
  queue.push("must disappear", "old");
  queue.clear();
  queue.push("new pane", "new");
  reject(Error("old connection closed"));
  await pending;
  assert.deepEqual(received, [
    ["already sent", "old"],
    ["new pane", "new"],
  ]);
  assert.equal(errors, 0);
});
