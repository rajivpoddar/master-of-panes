import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";

import { ASSIGNMENT_INLINE_TASK_MAX_BYTES, buildAssignmentTaskPacket } from "../src/assignmentTaskPacket.js";

test("small assignment files stay inline", () => {
  const contents = Buffer.from("Short task packet\n", "utf8");
  const packet = buildAssignmentTaskPacket("/tmp/task.md", contents);

  assert.equal(packet.mode, "file");
  assert.equal(packet.payload, contents);
  assert.equal(packet.taskBytes, contents.byteLength);
});

test("large assignment files use a FILE_PACKET reference with exact size and digest", () => {
  const contents = Buffer.alloc(5646, "x");
  const packet = buildAssignmentTaskPacket("/tmp/pm-delivered-archive/task-8297.md", contents);
  const reference = packet.payload.toString("utf8");
  const sha256 = createHash("sha256").update(contents).digest("hex");

  assert.equal(ASSIGNMENT_INLINE_TASK_MAX_BYTES, 4096);
  assert.equal(packet.mode, "file_ref");
  assert.equal(packet.taskBytes, 5646);
  assert.equal(packet.taskSha256, sha256);
  assert.match(reference, /FILE_PACKET path=\/tmp\/pm-delivered-archive\/task-8297\.md/);
  assert.match(reference, /bytes=5646/);
  assert.match(reference, new RegExp(`sha256=${sha256}`));
  assert.match(reference, /acknowledge path\+sha256, then execute its instructions/);
  assert.doesNotMatch(reference, /x{100}/, "the task body must never enter the composer inline");
});
