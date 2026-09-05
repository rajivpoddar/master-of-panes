#!/usr/bin/env python3
"""Deterministically validate the Issue Contract Ledger in an issue body."""

from __future__ import annotations

import argparse
import json
import re
import sys


PLACEHOLDER = re.compile(
    r"^(?:[-—–]|n/?a|none|tbd|todo|unknown|pending|fill(?:\s+(?:this|in)(?:\s+[^.]+)?)?|tests? pass(?:es)?)\.?$",
    re.I,
)

REQUIRED_ROWS: dict[str, tuple[str, ...]] = {
    "positive_ac": ("positive ac", "positive acs", "positive acceptance criteria", "positive acceptance criterion"),
    "negative_ac": ("negative ac", "negative acs", "negative acceptance criteria", "negative acceptance criterion"),
    "forbidden_implementation": (
        "forbidden implementation",
        "forbidden implementation path",
        "forbidden implementation paths",
        "forbidden paths",
    ),
    "determinism": (
        "determinism",
        "deterministic vs model dependent",
        "deterministic-vs-model-dependent",
    ),
    "required_proof": ("required proof", "required proof class", "required proof classes"),
}

AC_PROOF_FIELDS = {
    "surface": "surface",
    "qa reachability": "qa_reachability",
    "required proof": "required_proof",
    "qa scenario": "qa_scenario",
    "screenshot exemption": "screenshot_exemption",
}
ALLOWED_SURFACES = {"visual", "nonvisual"}
ALLOWED_REACHABILITY = {"deterministic", "nondeterministic", "not-applicable"}
ALLOWED_PROOF_CLASSES = {
    "artifact",
    "capture-replay",
    "e2e",
    "integration",
    "playwright",
    "screenshot",
    "telemetry",
    "trace",
    "unit",
}


def normalize(value: str) -> str:
    value = re.sub(r"[*_`]", "", value or "")
    value = re.sub(r"[^a-z0-9]+", " ", value.lower())
    return re.sub(r"\s+", " ", value).strip()


def ledger_section(body: str) -> str:
    heading = re.search(r"(?im)^##\s+Issue Contract Ledger\s*$", body or "")
    if not heading:
        return ""
    tail = (body or "")[heading.end() :]
    next_heading = re.search(r"(?m)^##\s+", tail)
    return tail[: next_heading.start()] if next_heading else tail


def acceptance_criteria_section(body: str) -> str:
    heading = re.search(
        r"(?im)^##\s+(?:Acceptance criteria|Acceptance criterion|ACs?)\s*$",
        body or "",
    )
    if not heading:
        return ""
    tail = (body or "")[heading.end() :]
    next_heading = re.search(r"(?m)^##\s+", tail)
    return tail[: next_heading.start()] if next_heading else tail


def qa_proof_schema_declared(body: str) -> bool:
    return bool(
        re.search(r"(?i)<!--\s*qa-proof-schema\s*:\s*1\s*-->", body or "")
        or
        re.search(
            r"(?ims)<!--\s*ready-pool:.*?^\s*qa_proof_schema\s*:\s*1\s*$.*?-->",
            body or "",
        )
    )


def parse_ac_proof_contract(body: str) -> list[dict[str, object]]:
    section = acceptance_criteria_section(body)
    criteria: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    ac_pattern = re.compile(
        r"^\s*(?:[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+)"
        r"(?P<id>AC-[A-Za-z0-9_-]+)\s*:\s*(?P<requirement>\S.*)$",
        re.I,
    )
    field_pattern = re.compile(
        r"^\s+(?:[-*+]\s+)?(?P<field>Surface|QA reachability|Required proof|"
        r"QA scenario|Screenshot exemption)\s*:\s*(?P<value>.*\S)\s*$",
        re.I,
    )
    for line in section.splitlines():
        ac_match = ac_pattern.match(line)
        if ac_match:
            current = {
                "id": ac_match.group("id").upper(),
                "requirement": ac_match.group("requirement").strip(),
                "fields": {},
            }
            criteria.append(current)
            continue
        field_match = field_pattern.match(line)
        if field_match and current is not None:
            key = AC_PROOF_FIELDS[normalize(field_match.group("field"))]
            fields = current["fields"]
            assert isinstance(fields, dict)
            fields[key] = field_match.group("value").strip()
    return criteria


def proof_classes(value: str) -> set[str]:
    return {
        normalize(item).replace(" ", "-")
        for item in re.split(r"\s*(?:,|\+|\band\b)\s*", value or "", flags=re.I)
        if normalize(item)
    }


def meaningful(value: str) -> bool:
    cleaned = re.sub(r"[*_`]", "", value or "").strip().strip("<>").strip()
    return bool(cleaned) and not PLACEHOLDER.fullmatch(cleaned)


def validate_ac_proof_contract(body: str) -> list[str]:
    criteria = parse_ac_proof_contract(body)
    if not criteria:
        return ["missing_ac_proof_contract"]

    errors: list[str] = []
    seen: set[str] = set()
    for criterion in criteria:
        ac_id = str(criterion["id"])
        fields = criterion["fields"]
        assert isinstance(fields, dict)
        if ac_id in seen:
            errors.append(f"duplicate_ac_id:{ac_id}")
        seen.add(ac_id)

        for field in ("surface", "qa_reachability", "required_proof"):
            if not meaningful(str(fields.get(field) or "")):
                errors.append(f"missing_ac_field:{ac_id}:{field}")

        surface = normalize(str(fields.get("surface") or "")).replace(" ", "")
        reachability = normalize(str(fields.get("qa_reachability") or "")).replace(" ", "-")
        proofs = proof_classes(str(fields.get("required_proof") or ""))
        unknown_proofs = sorted(proofs - ALLOWED_PROOF_CLASSES)
        if surface and surface not in ALLOWED_SURFACES:
            errors.append(f"invalid_ac_surface:{ac_id}:{surface}")
        if reachability and reachability not in ALLOWED_REACHABILITY:
            errors.append(f"invalid_ac_reachability:{ac_id}:{reachability}")
        for proof in unknown_proofs:
            errors.append(f"invalid_ac_proof_class:{ac_id}:{proof}")

        scenario = str(fields.get("qa_scenario") or "")
        exemption = str(fields.get("screenshot_exemption") or "")
        if surface == "visual" and reachability == "deterministic":
            if "screenshot" not in proofs:
                errors.append(f"visual_deterministic_ac_missing_screenshot:{ac_id}")
            if not meaningful(scenario):
                errors.append(f"visual_deterministic_ac_missing_qa_scenario:{ac_id}")
            if meaningful(exemption):
                errors.append(f"visual_deterministic_ac_has_exemption:{ac_id}")
        elif surface == "visual" and reachability == "nondeterministic":
            if not meaningful(exemption):
                errors.append(f"visual_nondeterministic_ac_missing_exemption:{ac_id}")
            if not (proofs - {"screenshot"}):
                errors.append(f"visual_nondeterministic_ac_missing_alternative_proof:{ac_id}")
        elif surface == "visual" and reachability == "not-applicable":
            errors.append(f"visual_ac_reachability_not_applicable:{ac_id}")
    return errors


def parse_rows(section: str) -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        key = normalize(cells[0])
        value = " | ".join(cells[1:]).strip()
        if not key or set(key) <= {"-"} or key in {"contract row", "requirement"}:
            continue
        rows[key] = value

    current_key = ""
    current_value: list[str] = []
    for line in section.splitlines():
        heading = re.match(r"^\s*(?:[-*+]\s+)?\*\*(.+?):\*\*\s*(.*)$", line)
        if heading:
            if current_key:
                rows[current_key] = " ".join(current_value).strip()
            current_key = normalize(heading.group(1))
            current_value = [heading.group(2).strip()] if heading.group(2).strip() else []
        elif current_key and line.strip():
            value = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", "", line.strip())
            current_value.append(value)
    if current_key:
        rows[current_key] = " ".join(current_value).strip()
    return rows


def row_value(rows: dict[str, str], aliases: tuple[str, ...]) -> str:
    normalized_aliases = [normalize(alias) for alias in aliases]
    for alias in normalized_aliases:
        if alias in rows:
            return rows[alias]
    for key, value in rows.items():
        if any(re.search(rf"(?:^| ){re.escape(alias)}(?: |$)", key) for alias in normalized_aliases):
            return value
    return ""


def validate(body: str, *, require_qa_proof: bool = False) -> list[str]:
    section = ledger_section(body)
    if not section:
        return ["missing_issue_contract_ledger"]
    rows = parse_rows(section)
    errors: list[str] = []
    for canonical, aliases in REQUIRED_ROWS.items():
        value = row_value(rows, aliases)
        cleaned = re.sub(r"[*_`]", "", value).strip().strip("<>").strip()
        if not cleaned:
            errors.append(f"missing_icl_row:{canonical}")
        elif PLACEHOLDER.fullmatch(cleaned):
            errors.append(f"placeholder_icl_row:{canonical}")
    if require_qa_proof or qa_proof_schema_declared(body):
        errors.extend(validate_ac_proof_contract(body))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--body-file")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--require-qa-proof", action="store_true")
    args = parser.parse_args()
    if args.body_file and args.body_file != "-":
        with open(args.body_file, encoding="utf-8") as handle:
            body = handle.read()
    else:
        body = sys.stdin.read()
    errors = validate(body, require_qa_proof=args.require_qa_proof)
    qa_contract = parse_ac_proof_contract(body)
    if args.json:
        payload: dict[str, object] = {"ok": not errors, "errors": errors}
        if args.require_qa_proof or qa_proof_schema_declared(body):
            payload["acceptance_criteria"] = qa_contract
        print(
            json.dumps(payload, sort_keys=True)
        )
    elif errors:
        print("INVALID_ISSUE_CONTRACT_LEDGER " + ",".join(errors))
    else:
        print("ISSUE_CONTRACT_LEDGER_OK")
    return 0 if not errors else 13


if __name__ == "__main__":
    raise SystemExit(main())
