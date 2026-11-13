# Scripted remote agent-browser scenarios

Run `python3 -I /path/to/scenarios/run.py /private/brief.json` ON THE OWNED QA BOX after AGENT_BROWSER_READY, within the existing capture controller. One script invocation runs every step; no LLM decisions between steps. Controller retains fetch, restoration and destruction ownership, including failures. Do not invoke this on the Mac.

Named scenarios:
- `clone-sample-open-editor`: internal sample clone → editor tab A → mounted, nonempty, writer-ready editor → screenshot.
- `editor-takeover-two-tabs`: base → same editor tab B in the SAME browser profile → real Continue editing here action and any required explicit confirmation → wait for B writer/A fenced → type → save → sync idle + changed head → authoritative server readback of edit and B lease → screenshot.

Brief shape (parameters are exact-head bindings resolved BEFORE recording):

```json
{
  "scenario": "editor-takeover-two-tabs",
  "parameters": {
    "owned_disposable_backend": true,
    "sampleId": "<owned seeded sample>",
    "editor_selector": "<exact-head editor selector>",
    "edit": "QA takeover persistence marker",
    "save_shortcut": "Control+s",
    "receipt": {
      "target_route": "<exact editor route/dialog the AC acts on>",
      "target_context": "<which tab/context performs each action>",
      "storage_scope": "<profile/storage per context, Convex deployment, QA R2 bucket scope>",
      "browser_contexts": [{"id": "A", "tab": 1, "subject": "<synthetic subject>", "profile": "<profile>"}]
    },
    "adapters": {
      "clone_internal": "/root/qa/bindings/clone-sample",
      "observe": "/root/qa/bindings/observe-editor"
    }
  }
}
```

`receipt` is recorded, never required: a missing `target_route`, `target_context`, `storage_scope` or `browser_contexts` is written as `UNKNOWN` and `partial_evidence: true` (with `unknown_fields`) in `scenario.json`; run.py does not refuse or change the scenario. The receipt is copied into `scenario.json`, and every action under test carries a `marker` (`tab`, `route`, `action`) beside its `elapsed_s`; qa-video-review maps AC steps to the clip through these markers. `editor-takeover-two-tabs` is one profile with two tabs, so its `browser_contexts` must say so: it does not satisfy an AC that needs two independent browser contexts for the same user (a different-browser writer move). Adapters are executable files bound to the exact head and owned disposable backend; no arbitrary shell strings, live Clerk or production writes. `clone_internal` accepts JSON `{sampleId}` on stdin, calls the EXISTING internal clone mutation with the synthetic owner, and returns `{transcriptId,editorUrl}`. Audit the actual function and argument schema first; do not guess an internal API or promote one. If that head has no internal clone path, return `SCENARIO_BINDING_MISSING` to PM; do not silently substitute a public mutation or different setup. Named customer-artifact ACs still require the exact artifact, not this sample.

`observe` accepts `{transcriptId,edit}` and returns JSON booleans `editor_ready`, `tab_a_writer`, `tab_b_ready`, `tab_b_readonly`, `tab_b_writer`, `tab_a_fenced`, `sync_idle`, plus `head_version`, `server_head_contains_edit`, `server_lease_is_tab_b`. UI fields inspect both real tabs; server fields query the authoritative head content and lease/session/device, not DOM text or guessed state. Never return static success. Keep this adapter deterministic, read-only and free of credentials in output. Missing observability is a typed blocker, not PASS.

All commands time out at 45s and state waits at 90s. State predicates are generic readiness, lease ownership, sync-idle and head-version change, NEVER exact-text waits; the final edit-content assertion is server verification, not a readiness predicate. Accessible button names are action locators, not save predicates. Record screenshots and timestamped command receipt; session-summary timing remains authoritative for wall/idle and stalls. Run named scenarios without agent reasoning between gestures. On timeout capture logs, end the controller nonzero, and confirm box + disposable-backend cleanup rather than leaving a broken box running.
