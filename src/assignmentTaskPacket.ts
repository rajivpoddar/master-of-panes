import { createHash } from "node:crypto";
import { basename } from "node:path";

export const ASSIGNMENT_INLINE_TASK_MAX_BYTES = 4 * 1024;

export interface AssignmentTaskPacket {
  mode: "file" | "file_ref";
  payload: Buffer;
  taskBytes: number;
  taskSha256: string;
}

/** Keep larger assignment bodies in their file and submit a verifiable pointer. */
export function buildAssignmentTaskPacket(filePath: string, contents: Buffer): AssignmentTaskPacket {
  const taskSha256 = createHash("sha256").update(contents).digest("hex");
  if (contents.byteLength <= ASSIGNMENT_INLINE_TASK_MAX_BYTES) {
    return { mode: "file", payload: contents, taskBytes: contents.byteLength, taskSha256 };
  }

  const pathToken = /\s/.test(filePath) ? JSON.stringify(filePath) : filePath;
  const source = basename(filePath).replace(/[^A-Za-z0-9._-]/g, "-").slice(0, 120) || "task.md";
  const reference = [
    `FILE_PACKET path=${pathToken} source=${source} bytes=${contents.byteLength} sha256=${taskSha256}.`,
    "Read this file with the Read tool, acknowledge path+sha256, then execute its instructions.",
    "The file body is not pasted inline.",
  ].join(" ");
  return {
    mode: "file_ref",
    payload: Buffer.from(reference, "utf8"),
    taskBytes: contents.byteLength,
    taskSha256,
  };
}
