#!/usr/bin/env python3
"""Focused proof for managed Codex CLI resolution and companion dispatch."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).parents[3]
SRC = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "skills" / "codex-review-companion" / "codex-review-companion.mjs"
MANIFEST = ROOT / "scripts" / "pm" / "shared-assets" / "manifest.json"
SOURCE_PATH = "claude/skills/codex-review-companion/codex-review-companion.mjs"
TARGET = "/Users/rajiv/.claude/skills/codex-review-companion/codex-review-companion.mjs"
CANONICAL = "/opt/homebrew/bin/codex"
MISSING_BUNDLE = "/Applications/ChatGPT.app/Contents/Resources/codex"
LIVE_MODEL = "gpt-6-luna"
LIVE_CONFIG = pathlib.Path("/Users/rajiv/.codex/config.toml")

NODE_PROBE = r"""
const companion = await import(process.argv[2]);
const mode = process.argv[3];
const input = JSON.parse(process.argv[4]);

if (mode === "resolve") {
  try {
    console.log(JSON.stringify({
      path: companion.resolveCodexBinary(input),
      model: companion.DEFAULT_MODEL,
    }));
  } catch (error) {
    console.log(JSON.stringify({error: String(error?.message || error)}));
  }
} else if (mode === "invoke") {
  const results = [];
  for (const reviewType of input.reviewTypes) {
    const result = await companion.invokeCodex(
      "fixture prompt",
      {repoRoot: input.repoRoot, reviewType, model: "gpt-5.5", effort: "high"},
    );
    results.push({reviewType, finalText: result.finalText});
  }
  console.log(JSON.stringify({results}));
} else if (mode === "missing") {
  const resolveMissing = () => companion.resolveCodexBinary({
    configuredBinary: input.configuredBinary,
    pathValue: input.pathValue,
    defaultBinary: null,
  });
  try {
    await companion.invokeCodex(
      "fixture prompt",
      {repoRoot: input.repoRoot, reviewType: "plan", markerFile: input.markerFile},
      resolveMissing,
    );
    console.log(JSON.stringify({error: null}));
  } catch (error) {
    console.log(JSON.stringify({error: String(error?.message || error)}));
  }
}
"""

FAKE_APP_SERVER = r"""
import fs from "node:fs";
import readline from "node:readline";

fs.appendFileSync(process.env.CODEX_TRACE_FILE, JSON.stringify({
  argv: process.argv.slice(2),
  configured: process.env.CODEX_BIN,
}) + "\n");
const rl = readline.createInterface({input: process.stdin});
const send = (value) => process.stdout.write(JSON.stringify(value) + "\n");
rl.on("line", (line) => {
  const request = JSON.parse(line);
  if (request.id === undefined) return;
  if (request.method === "initialize") {
    send({id: request.id, result: {}});
  } else if (request.method === "thread/start") {
    send({id: request.id, result: {thread: {id: "fixture-thread"}}});
  } else if (request.method === "turn/start") {
    send({id: request.id, result: {turn: {id: "fixture-turn", status: "inProgress"}}});
    setImmediate(() => {
      send({method: "item/completed", params: {
        threadId: "fixture-thread",
        item: {type: "agentMessage", phase: "final_answer", text: "APPROVE: fixture response"},
      }});
      send({method: "turn/completed", params: {
        threadId: "fixture-thread",
        turn: {status: "completed"},
      }});
    });
  }
});
rl.on("close", () => process.exit(0));
"""


def node_executable() -> str:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("node is required for the companion contract tests")
    return str(pathlib.Path(node).resolve())


def run_probe(module: pathlib.Path, mode: str, payload: dict, *, env: dict[str, str] | None = None):
    with tempfile.TemporaryDirectory() as directory:
        probe = pathlib.Path(directory) / "probe.mjs"
        probe.write_text(NODE_PROBE, encoding="utf-8")
        result = subprocess.run(
            [node_executable(), str(probe), str(module), mode, json.dumps(payload)],
            capture_output=True,
            text=True,
            timeout=20,
            env={**os.environ, **(env or {})},
        )
        return result


def write_executable(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def result_json(result: subprocess.CompletedProcess[str]) -> dict:
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError(f"probe returned no JSON; stderr={result.stderr[-1000:]}")
    return json.loads(lines[-1])


class ManagedBinaryResolutionTests(unittest.TestCase):
    def test_live_default_model_is_preserved(self):
        result = run_probe(SRC, "resolve", {
            "configuredBinary": MISSING_BUNDLE,
            "pathValue": "",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result_json(result)["model"], LIVE_MODEL)

    def test_manifest_maps_source_bytes_to_the_installed_companion(self):
        entries = json.loads(MANIFEST.read_text(encoding="utf-8"))["entries"]
        rows = [entry for entry in entries if entry["source_path"] == SOURCE_PATH]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["canonical_target"], TARGET)
        self.assertEqual(row["mode"], 493)
        self.assertEqual(row["dependency_status"], "closed")
        self.assertEqual(row["dependencies"], [])
        self.assertEqual(row["sha256"], hashlib.sha256(SRC.read_bytes()).hexdigest())

    def test_valid_configured_binary_precedes_path_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            configured = root / "configured-codex"
            path_binary = root / "path" / "codex"
            write_executable(configured, "#!/bin/sh\nexit 0\n")
            write_executable(path_binary, "#!/bin/sh\nexit 0\n")
            result = run_probe(SRC, "resolve", {
                "configuredBinary": str(configured),
                "pathValue": str(path_binary.parent),
            })
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result_json(result)["path"], str(configured))

    def test_missing_bundle_setting_falls_back_to_codex_on_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path_binary = pathlib.Path(directory) / "codex"
            write_executable(path_binary, "#!/bin/sh\nexit 0\n")
            result = run_probe(SRC, "resolve", {
                "configuredBinary": MISSING_BUNDLE,
                "pathValue": str(path_binary.parent),
            })
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result_json(result)["path"], str(path_binary))

    def test_brew_binary_is_the_last_resort_when_path_is_empty(self):
        result = run_probe(SRC, "resolve", {
            "configuredBinary": MISSING_BUNDLE,
            "pathValue": "",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result_json(result)["path"], CANONICAL)

    def test_no_executable_returns_a_typed_diagnostic(self):
        result = run_probe(SRC, "resolve", {
            "configuredBinary": MISSING_BUNDLE,
            "pathValue": "",
            "defaultBinary": None,
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CODEX_BIN_NOT_FOUND", result_json(result)["error"])

    def test_plan_and_code_invocations_use_path_binary_when_bundle_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            bin_dir = root / "bin"
            server = root / "fake-codex.mjs"
            trace = root / "spawn.jsonl"
            server.write_text(FAKE_APP_SERVER, encoding="utf-8")
            write_executable(
                bin_dir / "codex",
                f"#!/bin/sh\nexec {node_executable()!r} {str(server)!r} \"$@\"\n",
            )
            env = {
                "CODEX_BIN": MISSING_BUNDLE,
                "CODEX_TRACE_FILE": str(trace),
                "CODEX_TIMEOUT_MS": "4000",
                "PATH": str(bin_dir),
            }
            result = run_probe(SRC, "invoke", {
                "reviewTypes": ["plan", "code"],
                "repoRoot": str(root),
            }, env=env)
            self.assertEqual(result.returncode, 0, result.stderr[-1200:])
            self.assertEqual(result_json(result)["results"], [
                {"reviewType": "plan", "finalText": "APPROVE: fixture response"},
                {"reviewType": "code", "finalText": "APPROVE: fixture response"},
            ])
            launches = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(launches), 2)
            self.assertEqual([launch["argv"] for launch in launches], [["app-server"], ["app-server"]])
            self.assertTrue(all(launch["configured"] == MISSING_BUNDLE for launch in launches))

    def test_no_cli_rejects_invocation_without_writing_a_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            marker = root / "must-not-exist.txt"
            result = run_probe(SRC, "missing", {
                "configuredBinary": MISSING_BUNDLE,
                "pathValue": "",
                "repoRoot": str(root),
                "markerFile": str(marker),
            }, env={"CODEX_TIMEOUT_MS": "4000"})
            self.assertEqual(result.returncode, 0, result.stderr[-1200:])
            self.assertIn("CODEX_BIN_NOT_FOUND", result_json(result)["error"])
            self.assertFalse(marker.exists())


class LiveConfigCompatibilityTests(unittest.TestCase):
    """Read-only checks against the unchanged native Codex config."""

    def test_configured_missing_bundle_resolves_to_the_current_path_cli(self):
        result = run_probe(SRC, "resolve", {
            "configuredBinary": MISSING_BUNDLE,
            "pathValue": os.environ.get("PATH", ""),
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result_json(result)["path"], CANONICAL)

    def test_canonical_binary_parses_the_live_config(self):
        if not pathlib.Path(CANONICAL).exists():
            self.skipTest("canonical binary absent")
        result = subprocess.run([CANONICAL, "login", "status"], capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Logged in", result.stdout + result.stderr)
        self.assertTrue(LIVE_CONFIG.is_file())


if __name__ == "__main__":
    unittest.main()
