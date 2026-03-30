#!/usr/bin/env python3
from __future__ import annotations

"""Manage the external approved statement registry and generated Lean artifacts.

The registry is maintained in two layers:

1. JSON data under `approved_statement_registry_db/`
2. generated Lean files under `TestProject3/ApprovedStatementRegistry/`

Lean only reads the generated modules during compilation. This script is the
offline bridge that probes declarations through the root
`TestProject3.ApprovedStatementRegistry` module, updates JSON history, and
regenerates the Lean registry.
"""

import argparse
import copy
import json
import re
import subprocess
import tempfile
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_AUTHOR = "ai-agent"
SEVERITY_ORDER = {
    "clear": -1,
    "comment": 0,
    "warning": 1,
    "alert": 2,
}


@dataclass(frozen=True)
class RegistryPaths:
    """Resolved paths and build targets used by the registry tool."""

    project_root: Path
    data_root: Path
    current_dir: Path
    history_dir: Path
    lean_root: Path
    generated_file: Path
    shards_dir: Path
    build_target: str


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def make_event_id(action: str) -> str:
    return f"{now_utc_compact()}_{action}_{uuid.uuid4().hex[:8]}"


def chapter_section_label(chapter: int, section: int) -> str:
    return f"chapter_{chapter:02d}.section_{section:02d}"


def current_shard_filename(chapter: int, section: int) -> str:
    return f"approved_statement_registry.{chapter_section_label(chapter, section)}.json"


def lean_shard_stem(chapter: int, section: int) -> str:
    return f"ApprovedStatementRegistry_Chapter{chapter:02d}_Section{section:02d}"


def lean_shard_const(chapter: int, section: int) -> str:
    return f"approvedStatements_Chapter{chapter:02d}_Section{section:02d}"


def lean_shard_module(chapter: int, section: int) -> str:
    return f"TestProject3.ApprovedStatementRegistry.Shards.{lean_shard_stem(chapter, section)}"


def make_paths(project_root: Path) -> RegistryPaths:
    data_root = project_root / "approved_statement_registry_db"
    lean_root = project_root / "TestProject3" / "ApprovedStatementRegistry"
    return RegistryPaths(
        project_root=project_root,
        data_root=data_root,
        current_dir=data_root / "current",
        history_dir=data_root / "history",
        lean_root=lean_root,
        generated_file=lean_root / "Generated.lean",
        shards_dir=lean_root / "Shards",
        build_target="TestProject3.ApprovedStatementRegistry",
    )


def ensure_layout(paths: RegistryPaths) -> None:
    """Create the external DB layout and Lean shard directory if missing."""
    paths.current_dir.mkdir(parents=True, exist_ok=True)
    paths.history_dir.mkdir(parents=True, exist_ok=True)
    paths.shards_dir.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=False, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


SHARD_ID_RE = re.compile(r"^chapter_(\d+)\.section_(\d+)$")


def parse_shard_id(shard_id: str) -> tuple[int, int]:
    match = SHARD_ID_RE.match(shard_id)
    if match is None:
        raise SystemExit(f"Invalid shard id: {shard_id}")
    return (int(match.group(1)), int(match.group(2)))


def shard_ref(key: tuple[int, int]) -> dict[str, Any]:
    chapter, section = key
    return {
        "shard_id": chapter_section_label(chapter, section),
        "chapter": chapter,
        "section": section,
    }


def normalize_review_note(note: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": note["event_id"],
        "timestamp": note["timestamp"],
        "author": note["author"],
        "severity": note["severity"],
        "message": note["message"],
    }


def normalize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    review_notes = [normalize_review_note(note) for note in entry.get("review_notes", [])]
    normalized = {
        "decl_name": entry["decl_name"],
        "module": entry["module"],
        "statement_pretty": entry["statement_pretty"],
        "statement_hash": str(entry["statement_hash"]),
        "needs_human_review": bool(entry.get("needs_human_review", bool(review_notes))),
        "review_notes": review_notes,
        "review_status": entry.get("review_status", "clear"),
    }
    if "approved_by" in entry:
        normalized["approved_by"] = entry["approved_by"]
    if "approval_reason" in entry:
        normalized["approval_reason"] = entry["approval_reason"]
    if "created_at" in entry:
        normalized["created_at"] = entry["created_at"]
    if "updated_at" in entry:
        normalized["updated_at"] = entry["updated_at"]
    if "approved_at" in entry:
        normalized["approved_at"] = entry["approved_at"]
    return normalized


def normalize_shard_payload(payload: dict[str, Any]) -> dict[str, Any]:
    chapter = int(payload["chapter"])
    section = int(payload["section"])
    return {
        "schema_version": int(payload.get("schema_version", SCHEMA_VERSION)),
        "shard_id": chapter_section_label(chapter, section),
        "chapter": chapter,
        "section": section,
        "entries": [normalize_entry(entry) for entry in payload.get("entries", [])],
    }


def shard_key_from_payload(payload: dict[str, Any]) -> tuple[int, int]:
    return (int(payload["chapter"]), int(payload["section"]))


def infer_legacy_shard_ref(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    if "chapter" in entry and "section" in entry:
        chapter = int(entry["chapter"])
        section = int(entry["section"])
        return {
            "shard_id": chapter_section_label(chapter, section),
            "chapter": chapter,
            "section": section,
        }
    if "shard_id" in entry:
        chapter, section = parse_shard_id(entry["shard_id"])
        return {
            "shard_id": chapter_section_label(chapter, section),
            "chapter": chapter,
            "section": section,
        }
    return None


def normalize_history_change(change: dict[str, Any]) -> dict[str, Any]:
    before = None if change.get("before") is None else normalize_entry(change["before"])
    after = None if change.get("after") is None else normalize_entry(change["after"])
    before_shard = change.get("before_shard")
    after_shard = change.get("after_shard")
    normalized = {
        "decl_name": change["decl_name"],
        "kind": change["kind"],
    }
    effective_before_shard = before_shard or infer_legacy_shard_ref(change.get("before"))
    effective_after_shard = after_shard or infer_legacy_shard_ref(change.get("after"))
    if effective_before_shard is not None:
        normalized["before_shard"] = {
            "shard_id": effective_before_shard["shard_id"],
            "chapter": int(effective_before_shard["chapter"]),
            "section": int(effective_before_shard["section"]),
        }
    if effective_after_shard is not None:
        normalized["after_shard"] = {
            "shard_id": effective_after_shard["shard_id"],
            "chapter": int(effective_after_shard["chapter"]),
            "section": int(effective_after_shard["section"]),
        }
    normalized["before"] = before
    normalized["after"] = after
    return normalized


def normalize_history_event(event: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "schema_version": int(event.get("schema_version", SCHEMA_VERSION)),
        "event_id": event["event_id"],
        "action": event["action"],
        "timestamp": event["timestamp"],
        "author": event["author"],
        "reason": event["reason"],
    }
    if "rollback_of" in event:
        normalized["rollback_of"] = event["rollback_of"]
    normalized["changes"] = [normalize_history_change(change) for change in event.get("changes", [])]
    return normalized


def shard_key_from_ref(ref: dict[str, Any]) -> tuple[int, int]:
    return (int(ref["chapter"]), int(ref["section"]))


def make_change(
    *,
    decl_name: str,
    kind: str,
    before_key: tuple[int, int] | None,
    before: dict[str, Any] | None,
    after_key: tuple[int, int] | None,
    after: dict[str, Any] | None,
) -> dict[str, Any]:
    change = {
        "decl_name": decl_name,
        "kind": kind,
    }
    if before_key is not None:
        change["before_shard"] = shard_ref(before_key)
    if after_key is not None:
        change["after_shard"] = shard_ref(after_key)
    change["before"] = None if before is None else normalize_entry(before)
    change["after"] = None if after is None else normalize_entry(after)
    return change


def default_shard_payload(chapter: int, section: int) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "shard_id": chapter_section_label(chapter, section),
        "chapter": chapter,
        "section": section,
        "entries": [],
    }


def load_current_shards(paths: RegistryPaths) -> dict[tuple[int, int], dict[str, Any]]:
    ensure_layout(paths)
    shards: dict[tuple[int, int], dict[str, Any]] = {}
    for path in sorted(paths.current_dir.glob("*.json")):
        payload = normalize_shard_payload(read_json(path))
        key = shard_key_from_payload(payload)
        shards[key] = payload
    return shards


def save_current_shards(paths: RegistryPaths, shards: dict[tuple[int, int], dict[str, Any]]) -> None:
    """Rewrite current JSON shards and delete empty shard files."""
    ensure_layout(paths)
    wanted_files: set[Path] = set()
    for key, payload in sorted(shards.items()):
        chapter, section = key
        entries = sorted((normalize_entry(entry) for entry in payload["entries"]), key=lambda entry: entry["decl_name"])
        normalized_payload = {
            "schema_version": SCHEMA_VERSION,
            "shard_id": chapter_section_label(chapter, section),
            "chapter": chapter,
            "section": section,
            "entries": entries,
        }
        path = paths.current_dir / current_shard_filename(*key)
        if entries:
            wanted_files.add(path)
            write_json(path, normalized_payload)
        elif path.exists():
            path.unlink()
    for path in paths.current_dir.glob("*.json"):
        if path not in wanted_files:
            path.unlink()


def index_entries(shards: dict[tuple[int, int], dict[str, Any]]) -> dict[str, tuple[tuple[int, int], dict[str, Any]]]:
    """Build a declaration-name index over the current registry snapshot."""
    index: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
    for key, payload in shards.items():
        for entry in payload["entries"]:
            decl_name = entry["decl_name"]
            if decl_name in index:
                raise SystemExit(f"Duplicate declaration in approved statement registry: {decl_name}")
            index[decl_name] = (key, entry)
    return index


def recompute_review_state(entry: dict[str, Any]) -> None:
    """Derive aggregate review status from the accumulated review notes."""
    highest = "clear"
    for note in entry.get("review_notes", []):
        severity = note["severity"]
        if SEVERITY_ORDER[severity] > SEVERITY_ORDER[highest]:
            highest = severity
    entry["review_status"] = highest
    entry["needs_human_review"] = bool(entry.get("review_notes"))


def latest_review_note(entry: dict[str, Any]) -> dict[str, Any] | None:
    notes = entry.get("review_notes", [])
    if not notes:
        return None
    return notes[-1]


def severity_meets_threshold(actual: str, threshold: str) -> bool:
    return SEVERITY_ORDER[actual] >= SEVERITY_ORDER[threshold]


def make_review_note(
    event_id: str,
    timestamp: str,
    author: str,
    severity: str,
    message: str,
) -> dict[str, str]:
    return {
        "event_id": event_id,
        "timestamp": timestamp,
        "author": author,
        "severity": severity,
        "message": message,
    }


def run_command(paths: RegistryPaths, args: list[str], description: str) -> subprocess.CompletedProcess[str]:
    """Run a subprocess from the project root and surface a concise error."""
    result = subprocess.run(
        args,
        cwd=paths.project_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
        raise SystemExit(f"{description} failed:\n{output}")
    return result


def probe_declarations(paths: RegistryPaths, module_name: str, decl_names: list[str]) -> list[dict[str, str]]:
    """Ask Lean to elaborate declarations and print their normalized statement hashes."""
    if not decl_names:
        return []
    lines = [
        # The registry root module exposes the offline probe command.
        f"import {paths.build_target}",
        f"import {module_name}",
        "",
    ]
    lines.extend(f"#print_approved_statement_probe {decl_name}" for decl_name in decl_names)
    source = "\n".join(lines) + "\n"
    with tempfile.NamedTemporaryFile("w", suffix=".lean", encoding="utf-8", delete=False) as handle:
        handle.write(source)
        temp_path = Path(handle.name)
    try:
        result = run_command(paths, ["lake", "env", "lean", str(temp_path)], f"Probe module {module_name}")
    finally:
        temp_path.unlink(missing_ok=True)

    payloads: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            payloads.append(json.loads(stripped))
    if len(payloads) != len(decl_names):
        raise SystemExit(
            f"Expected {len(decl_names)} probe results from module {module_name}, got {len(payloads)}.\n"
            f"Probe output was:\n{result.stdout.strip()}"
        )
    return payloads

def upsert_entry(
    shards: dict[tuple[int, int], dict[str, Any]],
    key: tuple[int, int],
    entry: dict[str, Any],
) -> None:
    """Insert or replace a declaration inside one chapter/section shard."""
    shard = shards.setdefault(key, default_shard_payload(*key))
    shard["entries"] = [existing for existing in shard["entries"] if existing["decl_name"] != entry["decl_name"]]
    shard["entries"].append(entry)


def remove_entry(
    shards: dict[tuple[int, int], dict[str, Any]],
    key: tuple[int, int],
    decl_name: str,
) -> None:
    shard = shards[key]
    shard["entries"] = [entry for entry in shard["entries"] if entry["decl_name"] != decl_name]
    if not shard["entries"]:
        del shards[key]


def generate_lean_registry(paths: RegistryPaths) -> None:
    """Regenerate chapter/section shard modules plus the aggregate generated module."""
    ensure_layout(paths)
    shards = load_current_shards(paths)
    wanted_files: set[Path] = set()
    imports: list[str] = []
    consts: list[str] = []

    for (chapter, section), payload in sorted(shards.items()):
        if not payload["entries"]:
            continue
        stem = lean_shard_stem(chapter, section)
        const_name = lean_shard_const(chapter, section)
        shard_path = paths.shards_dir / f"{stem}.lean"
        wanted_files.add(shard_path)
        imports.append(f"import {lean_shard_module(chapter, section)}")
        consts.append(const_name)
        entry_lines = []
        for entry in payload["entries"]:
            entry_lines.append(
                "  {\n"
                f"    name := `{entry['decl_name']}\n"
                f'    shardId := "{payload["shard_id"]}"\n'
                f'    statementHash := ({entry["statement_hash"]} : UInt64)\n'
                "  }"
            )
        entries_block = ",\n".join(entry_lines)
        shard_source = (
            "/- Auto-generated approved-statement shard. Do not edit by hand. -/\n"
            "import TestProject3.ApprovedStatementRegistry.Types\n\n"
            "namespace TestProject3.ApprovedStatementRegistry\n\n"
            f"def {const_name} : Array ApprovedStatement := #[\n"
            f"{entries_block}\n"
            "]\n\n"
            "end TestProject3.ApprovedStatementRegistry\n"
        )
        shard_path.write_text(shard_source, encoding="utf-8")

    for path in paths.shards_dir.glob("*.lean"):
        if path not in wanted_files:
            path.unlink()

    import_block = "\n".join(["import TestProject3.ApprovedStatementRegistry.Types", *imports])
    if consts:
        body = " ++\n  ".join(consts)
        generated_source = (
            "/- Auto-generated registry aggregate. Do not edit by hand. -/\n"
            f"{import_block}\n\n"
            "namespace TestProject3.ApprovedStatementRegistry\n\n"
            "def generatedApprovedStatements : Array ApprovedStatement :=\n"
            f"  {body}\n\n"
            "end TestProject3.ApprovedStatementRegistry\n"
        )
    else:
        generated_source = (
            "/- Auto-generated registry aggregate. Do not edit by hand. -/\n"
            "import TestProject3.ApprovedStatementRegistry.Types\n\n"
            "namespace TestProject3.ApprovedStatementRegistry\n\n"
            "def generatedApprovedStatements : Array ApprovedStatement := #[]\n\n"
            "end TestProject3.ApprovedStatementRegistry\n"
        )
    paths.generated_file.write_text(generated_source, encoding="utf-8")


def maybe_build_registry(paths: RegistryPaths, skip_build: bool) -> None:
    if skip_build:
        return
    # The root registry module imports the generated aggregate, so rebuilding
    # this single target refreshes both the probe command and the current shard
    # snapshot.
    run_command(paths, ["lake", "build", paths.build_target], f"Build {paths.build_target}")


def approve_entries(args: argparse.Namespace, paths: RegistryPaths) -> None:
    """Freeze one or more declaration statements into a chapter/section shard."""
    run_command(paths, ["lake", "build", paths.build_target], f"Build {paths.build_target}")
    probed = probe_declarations(paths, args.module, args.decl)
    shards = load_current_shards(paths)
    index = index_entries(shards)
    timestamp = now_utc_iso()
    event_id = make_event_id("approve")
    changes: list[dict[str, Any]] = []
    target_key = (args.chapter, args.section)
    # `reason` is optional on the CLI; keep a stable default so history remains
    # readable even for fully automated agent runs.
    reason = args.reason or "approved statement freeze"

    for payload in probed:
        decl_name = payload["decl_name"]
        before = copy.deepcopy(index[decl_name][1]) if decl_name in index else None
        if decl_name in index:
            old_key = index[decl_name][0]
            if old_key != target_key:
                remove_entry(shards, old_key, decl_name)
        review_notes = copy.deepcopy(before["review_notes"]) if before else []
        if before and before["statement_hash"] != payload["statement_hash"]:
            review_notes.append(
                make_review_note(
                    event_id=event_id,
                    timestamp=timestamp,
                    author=args.author,
                    severity="warning",
                    message=(
                        "Statement hash changed during approve from "
                        f"{before['statement_hash']} to {payload['statement_hash']}. "
                        "Manual review recommended."
                    ),
                )
            )
        after = {
            "decl_name": decl_name,
            "module": args.module,
            "statement_pretty": payload["statement_pretty"],
            "statement_hash": payload["statement_hash"],
            "needs_human_review": False,
            "review_notes": review_notes,
            "review_status": "clear",
            "approved_by": args.author,
            "approval_reason": reason,
            "created_at": before["created_at"] if before else timestamp,
            "updated_at": timestamp,
            "approved_at": timestamp,
        }
        recompute_review_state(after)
        upsert_entry(shards, target_key, after)
        before_key = None if before is None else index[decl_name][0]
        changes.append(make_change(
            decl_name=decl_name,
            kind="added" if before is None else "updated",
            before_key=before_key,
            before=before,
            after_key=target_key,
            after=after,
        ))

    save_current_shards(paths, shards)
    write_json(
        paths.history_dir / f"{event_id}.json",
        normalize_history_event(
            {
                "schema_version": SCHEMA_VERSION,
                "event_id": event_id,
                "action": "approve",
                "timestamp": timestamp,
                "author": args.author,
                "reason": reason,
                "changes": changes,
            }
        ),
    )
    generate_lean_registry(paths)
    maybe_build_registry(paths, args.skip_build)
    print(f"Approved {len(changes)} declaration(s) into {chapter_section_label(args.chapter, args.section)}.")


def add_commit_notes(args: argparse.Namespace, paths: RegistryPaths) -> None:
    """Append human-review notes to already approved declarations."""
    shards = load_current_shards(paths)
    index = index_entries(shards)
    timestamp = now_utc_iso()
    event_id = make_event_id("commit")
    changes: list[dict[str, Any]] = []

    for decl_name in args.decl:
        if decl_name not in index:
            raise SystemExit(f"Declaration is not present in the approved statement registry: {decl_name}")
        key, current = index[decl_name]
        before = copy.deepcopy(current)
        note = make_review_note(event_id, timestamp, args.author, args.severity, args.message)
        current["review_notes"] = copy.deepcopy(current.get("review_notes", []))
        current["review_notes"].append(note)
        current["updated_at"] = timestamp
        recompute_review_state(current)
        changes.append(make_change(
            decl_name=decl_name,
            kind="annotated",
            before_key=key,
            before=before,
            after_key=key,
            after=copy.deepcopy(current),
        ))
        upsert_entry(shards, key, current)

    save_current_shards(paths, shards)
    write_json(
        paths.history_dir / f"{event_id}.json",
        normalize_history_event(
            {
                "schema_version": SCHEMA_VERSION,
                "event_id": event_id,
                "action": "commit",
                "timestamp": timestamp,
                "author": args.author,
                "reason": args.reason or args.message,
                "changes": changes,
            }
        ),
    )
    print(f"Recorded {args.severity} note for {len(changes)} declaration(s).")


def prune_entries(args: argparse.Namespace, paths: RegistryPaths) -> None:
    """Remove declarations from the approved registry and regenerate Lean data."""
    shards = load_current_shards(paths)
    index = index_entries(shards)
    timestamp = now_utc_iso()
    event_id = make_event_id("prune")
    changes: list[dict[str, Any]] = []

    for decl_name in args.decl:
        if decl_name not in index:
            raise SystemExit(f"Declaration is not present in the approved statement registry: {decl_name}")
        key, entry = index[decl_name]
        before = copy.deepcopy(entry)
        remove_entry(shards, key, decl_name)
        changes.append(make_change(
            decl_name=decl_name,
            kind="removed",
            before_key=key,
            before=before,
            after_key=None,
            after=None,
        ))

    save_current_shards(paths, shards)
    write_json(
        paths.history_dir / f"{event_id}.json",
        normalize_history_event(
            {
                "schema_version": SCHEMA_VERSION,
                "event_id": event_id,
                "action": "prune",
                "timestamp": timestamp,
                "author": args.author,
                "reason": args.reason or "removed from approved statement registry",
                "changes": changes,
            }
        ),
    )
    generate_lean_registry(paths)
    maybe_build_registry(paths, args.skip_build)
    print(f"Pruned {len(changes)} declaration(s).")


def rollback_event(args: argparse.Namespace, paths: RegistryPaths) -> None:
    """Replay the inverse of one history event and persist a rollback event."""
    event_path = paths.history_dir / f"{args.event_id}.json"
    if not event_path.exists():
        raise SystemExit(f"No history event found: {args.event_id}")
    target_event = normalize_history_event(read_json(event_path))
    shards = load_current_shards(paths)
    timestamp = now_utc_iso()
    rollback_id = make_event_id("rollback")
    changes: list[dict[str, Any]] = []

    for change in reversed(target_event["changes"]):
        decl_name = change["decl_name"]
        before = change["before"]
        after = change["after"]
        before_shard = change.get("before_shard")
        after_shard = change.get("after_shard")
        current_index = index_entries(shards)
        current_before = copy.deepcopy(current_index[decl_name][1]) if decl_name in current_index else None
        current_before_key = current_index[decl_name][0] if decl_name in current_index else None

        if after is None and before is not None:
            if before_shard is None:
                raise SystemExit(f"Rollback metadata missing before_shard for {decl_name}")
            target_key = shard_key_from_ref(before_shard)
            upsert_entry(shards, target_key, copy.deepcopy(before))
            current_after = copy.deepcopy(before)
            current_after_key = target_key
        elif before is None and after is not None:
            if decl_name in current_index:
                remove_entry(shards, current_index[decl_name][0], decl_name)
            current_after = None
            current_after_key = None
        elif before is not None and after is not None:
            if before_shard is None:
                raise SystemExit(f"Rollback metadata missing before_shard for {decl_name}")
            if decl_name in current_index:
                remove_entry(shards, current_index[decl_name][0], decl_name)
            target_key = shard_key_from_ref(before_shard)
            upsert_entry(shards, target_key, copy.deepcopy(before))
            current_after = copy.deepcopy(before)
            current_after_key = target_key
        else:
            continue

        changes.append(make_change(
            decl_name=decl_name,
            kind="rolled_back",
            before_key=current_before_key,
            before=current_before,
            after_key=current_after_key,
            after=current_after,
        ))

    save_current_shards(paths, shards)
    write_json(
        paths.history_dir / f"{rollback_id}.json",
        normalize_history_event(
            {
                "schema_version": SCHEMA_VERSION,
                "event_id": rollback_id,
                "action": "rollback",
                "timestamp": timestamp,
                "author": args.author,
                "reason": args.reason or f"rollback of {args.event_id}",
                "rollback_of": args.event_id,
                "changes": changes,
            }
        ),
    )
    generate_lean_registry(paths)
    maybe_build_registry(paths, args.skip_build)
    print(f"Rolled back history event {args.event_id}.")


def audit_registry(args: argparse.Namespace, paths: RegistryPaths) -> None:
    """Compare stored hashes to freshly probed declarations and report review notes."""
    run_command(paths, ["lake", "build", paths.build_target], f"Build {paths.build_target}")
    shards = load_current_shards(paths)
    index = index_entries(shards)
    selected_names = args.decl or sorted(index.keys())
    entries: list[dict[str, Any]] = []
    for decl_name in selected_names:
        if decl_name not in index:
            raise SystemExit(f"Declaration is not present in the approved statement registry: {decl_name}")
        entries.append(copy.deepcopy(index[decl_name][1]))

    grouped: dict[str, list[str]] = defaultdict(list)
    for entry in entries:
        grouped[entry["module"]].append(entry["decl_name"])

    problems: list[str] = []
    for module_name, decl_names in sorted(grouped.items()):
        actual_by_name = {
            payload["decl_name"]: payload
            for payload in probe_declarations(paths, module_name, decl_names)
        }
        for decl_name in decl_names:
            entry = index[decl_name][1]
            actual = actual_by_name[decl_name]
            if entry["statement_hash"] != actual["statement_hash"]:
                problems.append(
                    f"{decl_name}: stored hash {entry['statement_hash']} but current hash is {actual['statement_hash']}"
                )

    if problems:
        print("Approved statement registry audit failed:")
        for problem in problems:
            print(f"- {problem}")
        raise SystemExit(1)

    flagged_entries = [entry for entry in entries if entry.get("review_status", "clear") != "clear"]
    if flagged_entries:
        print("Approved statement registry review notes:")
        for entry in flagged_entries:
            note = latest_review_note(entry)
            if note is None:
                continue
            print(
                f"- {entry['decl_name']} [{entry['review_status']}] "
                f"{note['timestamp']} {note['author']}: {note['message']}"
            )

    if args.fail_on_review_status is not None:
        blockers = [
            entry["decl_name"]
            for entry in flagged_entries
            if severity_meets_threshold(entry["review_status"], args.fail_on_review_status)
        ]
        if blockers:
            print(
                "Approved statement registry audit failed because review-note severity "
                f"reached {args.fail_on_review_status}:"
            )
            for decl_name in blockers:
                print(f"- {decl_name}")
            raise SystemExit(1)

    print(f"Approved statement registry audit passed for {len(entries)} declaration(s).")


def print_history(args: argparse.Namespace, paths: RegistryPaths) -> None:
    """Print recent registry events, including newly added review notes."""
    decl_filter = set(args.decl)
    events = []
    for path in sorted(paths.history_dir.glob("*.json"), reverse=True):
        event = normalize_history_event(read_json(path))
        if decl_filter and not any(change["decl_name"] in decl_filter for change in event["changes"]):
            continue
        events.append(event)
    if args.limit is not None:
        events = events[: args.limit]
    for event in events:
        print(
            f"{event['event_id']}  action={event['action']}  author={event['author']}  "
            f"timestamp={event['timestamp']}  reason={event['reason']}"
        )
        for change in event["changes"]:
            print(f"  - {change['decl_name']} [{change['kind']}]")
            before = change.get("before")
            after = change.get("after")
            before_notes = [] if before is None else before.get("review_notes", [])
            after_notes = [] if after is None else after.get("review_notes", [])
            if len(after_notes) > len(before_notes):
                note = after_notes[-1]
                print(f"    note[{note['severity']}] {note['author']}: {note['message']}")


def regenerate_registry(args: argparse.Namespace, paths: RegistryPaths) -> None:
    """Rebuild Lean registry files from the current JSON snapshot."""
    generate_lean_registry(paths)
    maybe_build_registry(paths, args.skip_build)
    print("Regenerated Lean approved statement registry artifacts.")


def parse_args() -> tuple[argparse.Namespace, RegistryPaths]:
    parser = argparse.ArgumentParser(
        description="Manage the external approved statement registry and generated Lean artifacts."
    )
    parser.add_argument("--project-root", default=".", help="Path to the Lean project root.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    approve = subparsers.add_parser("approve", help="Approve declarations into a chapter/section shard.")
    approve.add_argument("--module", required=True, help="Lean module importing the declarations to probe.")
    approve.add_argument("--chapter", type=int, required=True, help="Book chapter number for the registry shard.")
    approve.add_argument("--section", type=int, required=True, help="Book section number for the registry shard.")
    approve.add_argument(
        "--decl",
        action="append",
        required=True,
        help="Declaration name to approve. Repeatable, so one command can import multiple theorems.",
    )
    approve.add_argument(
        "--reason",
        help="Optional approval reason recorded in history. Defaults to 'approved statement freeze'.",
    )
    approve.add_argument("--author", default=DEFAULT_AUTHOR, help="Author recorded in the registry history.")
    approve.add_argument("--skip-build", action="store_true", help="Skip rebuilding the Lean registry target.")

    commit = subparsers.add_parser("commit", help="Attach a review comment or alert to approved declarations.")
    commit.add_argument("--decl", action="append", required=True, help="Approved declaration name. Repeatable.")
    commit.add_argument("--message", required=True, help="Comment or warning message for human review.")
    commit.add_argument(
        "--severity",
        choices=["comment", "warning", "alert"],
        default="warning",
        help="Review note severity.",
    )
    commit.add_argument("--reason", help="Commit reason recorded in the registry history.")
    commit.add_argument("--author", default=DEFAULT_AUTHOR, help="Author recorded in the registry history.")

    prune = subparsers.add_parser("prune", help="Remove declarations from the approved statement registry.")
    prune.add_argument("--decl", action="append", required=True, help="Approved declaration name. Repeatable.")
    prune.add_argument("--reason", help="Removal reason recorded in the registry history.")
    prune.add_argument("--author", default=DEFAULT_AUTHOR, help="Author recorded in the registry history.")
    prune.add_argument("--skip-build", action="store_true", help="Skip rebuilding the Lean registry target.")

    rollback = subparsers.add_parser("rollback", help="Rollback a registry history event by id.")
    rollback.add_argument("--event-id", required=True, help="History event id to rollback.")
    rollback.add_argument("--reason", help="Rollback reason recorded in the registry history.")
    rollback.add_argument("--author", default=DEFAULT_AUTHOR, help="Author recorded in the registry history.")
    rollback.add_argument("--skip-build", action="store_true", help="Skip rebuilding the Lean registry target.")

    audit = subparsers.add_parser("audit", help="Probe current declarations and compare them to the registry.")
    audit.add_argument("--decl", action="append", default=[], help="Approved declaration name filter. Repeatable.")
    audit.add_argument(
        "--fail-on-review-status",
        choices=["comment", "warning", "alert"],
        help="Fail if any selected declaration has review status at or above this severity.",
    )

    history = subparsers.add_parser("history", help="Print registry history.")
    history.add_argument("--decl", action="append", default=[], help="Declaration name filter. Repeatable.")
    history.add_argument("--limit", type=int, help="Maximum number of history events to print.")

    generate = subparsers.add_parser("generate", help="Regenerate Lean registry files from current JSON data.")
    generate.add_argument("--skip-build", action="store_true", help="Skip rebuilding the Lean registry target.")

    args = parser.parse_args()
    paths = make_paths(Path(args.project_root).resolve())
    ensure_layout(paths)
    return args, paths


def main() -> None:
    args, paths = parse_args()
    if args.command == "approve":
        approve_entries(args, paths)
    elif args.command == "commit":
        add_commit_notes(args, paths)
    elif args.command == "prune":
        prune_entries(args, paths)
    elif args.command == "rollback":
        rollback_event(args, paths)
    elif args.command == "audit":
        audit_registry(args, paths)
    elif args.command == "history":
        print_history(args, paths)
    elif args.command == "generate":
        regenerate_registry(args, paths)
    else:
        raise SystemExit(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    main()
