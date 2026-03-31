from __future__ import annotations

import argparse
import copy
from collections import defaultdict
from pathlib import Path

from .common import DEFAULT_AUTHOR, STATUS_CHOICES, make_paths, now_utc_iso, shard_ref, status_meets_threshold
from .db import (
    index_entries,
    iter_entries,
    load_current_shards,
    load_history_records,
    save_current_shards,
    select_history_records,
    update_entry_commit,
    upsert_entry,
    remove_entry,
    write_history_record,
)
from .lean_ops import (
    build_registry,
    generate_lean_registry,
    probe_declarations,
    run_command,
    run_temporary_axiom_audit,
)


def fail_missing_registry_decl(decl_name: str, *, action: str) -> None:
    raise SystemExit(
        f"`{action}` 无法继续，因为 `{decl_name}` 不在已批准陈述注册库 current 快照中。"
    )


def ensure_unique_values(values: list[str], *, flag: str) -> None:
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    if duplicates:
        raise SystemExit(f"`{flag}` 不允许重复值：{', '.join(duplicates)}")


def approve_entries(args: argparse.Namespace, paths) -> None:
    run_command(paths, ["lake", "build", paths.build_target], f"构建 `{paths.build_target}`")
    probed = probe_declarations(paths, args.module, args.decl)
    shards = load_current_shards(paths)
    index = index_entries(shards)
    timestamp = now_utc_iso()
    target_key = (args.chapter, args.section)
    history_count = 0
    for payload in probed:
        decl_name = payload["decl_name"]
        before = copy.deepcopy(index[decl_name][1]) if decl_name in index else None
        before_key = index[decl_name][0] if decl_name in index else None
        if before_key is not None and before_key != target_key:
            remove_entry(shards, before_key, decl_name)
        hash_changed = before is not None and before["statement_hash"] != payload["statement_hash"]
        after = {
            "decl_name": decl_name,
            "module": args.module,
            "statement_pretty": payload["statement_pretty"],
            "statement_hash": payload["statement_hash"],
            "status": "needs_attention" if hash_changed else (before["status"] if before else "safe"),
            "commit": [] if hash_changed else (copy.deepcopy(before["commit"]) if before else []),
            "approved_by": args.author,
            "approval_reason": args.reason or "approved statement freeze",
            "created_at": before["created_at"] if before else timestamp,
            "updated_at": timestamp,
            "approved_at": timestamp,
        }
        upsert_entry(shards, target_key, after)
        if hash_changed:
            history_count += 1
            write_history_record(
                paths,
                timestamp=timestamp,
                decl_name=decl_name,
                before_key=before_key,
                before=before,
                after_key=target_key,
                after=after,
            )
    save_current_shards(paths, shards)
    generate_lean_registry(paths)
    build_registry(paths)
    print(f"Approved {len(probed)} declaration(s) into {args.chapter:02d}.{args.section:02d}.")
    if history_count > 0:
        print(f"Recorded {history_count} statement-history update(s).")


def validate_commit_args(args: argparse.Namespace) -> None:
    if args.append and args.message is None:
        raise SystemExit("`commit --append` requires `--message`.")
    if args.clear and args.drop is not None:
        raise SystemExit("`commit` does not allow `--clear` and `--drop` together.")
    if args.message is not None and (args.clear or args.drop is not None):
        raise SystemExit("`commit` does not allow `--message` together with `--clear` or `--drop`.")
    if args.drop is not None and args.drop < 1:
        raise SystemExit("`commit --drop` expects a 1-based positive index.")
    if args.status is None and args.message is None and not args.clear and args.drop is None:
        raise SystemExit("`commit` needs at least one of `--status`, `--message`, `--clear`, or `--drop`.")


def update_commit_and_status(args: argparse.Namespace, paths) -> None:
    validate_commit_args(args)
    shards = load_current_shards(paths)
    index = index_entries(shards)
    timestamp = now_utc_iso()
    for decl_name in args.decl:
        if decl_name not in index:
            fail_missing_registry_decl(decl_name, action="commit")
        key, current = index[decl_name]
        updated = copy.deepcopy(current)
        update_entry_commit(
            updated,
            timestamp=timestamp,
            author=args.author,
            message=args.message,
            append=args.append,
            clear=args.clear,
            drop=args.drop,
        )
        if args.status is not None:
            updated["status"] = args.status
        updated["updated_at"] = timestamp
        upsert_entry(shards, key, updated)
    save_current_shards(paths, shards)
    print(f"Updated commit/status metadata for {len(args.decl)} declaration(s).")


def prune_entries(args: argparse.Namespace, paths) -> None:
    shards = load_current_shards(paths)
    index = index_entries(shards)
    removed = 0
    for decl_name in args.decl:
        if decl_name not in index:
            fail_missing_registry_decl(decl_name, action="prune")
        key, _ = index[decl_name]
        remove_entry(shards, key, decl_name)
        removed += 1
    save_current_shards(paths, shards)
    generate_lean_registry(paths)
    build_registry(paths)
    print(f"Pruned {removed} declaration(s).")


def audit_registry(args: argparse.Namespace, paths) -> None:
    run_command(paths, ["lake", "build", paths.build_target], f"构建 `{paths.build_target}`")
    shards = load_current_shards(paths)
    index = index_entries(shards)
    selected_names = args.decl or sorted(index.keys())
    entries: list[dict[str, str]] = []
    for decl_name in selected_names:
        if decl_name not in index:
            fail_missing_registry_decl(decl_name, action="audit")
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
        raise SystemExit(
            "approved statement registry 审计失败：\n" + "\n".join(f"- {problem}" for problem in problems)
        )
    flagged_entries = [
        entry for entry in entries if entry.get("status", "safe") != "safe" or entry.get("commit", [])
    ]
    if flagged_entries:
        print("Registry metadata flags:")
        for entry in flagged_entries:
            print(
                f"- {entry['decl_name']} [{entry['status']}] commits={len(entry.get('commit', []))}"
            )
    if args.fail_on_status is not None:
        blockers = [
            entry["decl_name"]
            for entry in entries
            if status_meets_threshold(entry["status"], args.fail_on_status)
        ]
        if blockers:
            raise SystemExit(
                "approved statement registry 审计因 status 触发失败：\n"
                + "\n".join(f"- {decl_name}" for decl_name in blockers)
            )
    print(f"Approved statement registry audit passed for {len(entries)} declaration(s).")


def format_commit_items(entry: dict[str, str]) -> list[str]:
    lines: list[str] = []
    for idx, item in enumerate(entry.get("commit", []), start=1):
        prefix = f"{idx}. "
        meta = " ".join(part for part in [item.get("timestamp", ""), item.get("author", "")] if part)
        if meta:
            lines.append(f"{prefix}{meta}: {item['message']}")
        else:
            lines.append(f"{prefix}{item['message']}")
    return lines


def report_registry(args: argparse.Namespace, paths) -> None:
    shards = load_current_shards(paths)
    status_filter = set(args.status)
    selected: list[tuple[tuple[int, int], dict[str, str]]] = []
    for key, entry in iter_entries(shards):
        if args.decl and entry["decl_name"] not in args.decl:
            continue
        if status_filter and entry["status"] not in status_filter:
            continue
        if not args.all and not args.decl and not status_filter and not entry.get("commit", []):
            continue
        selected.append((key, copy.deepcopy(entry)))
    for key, entry in selected:
        print(f"{entry['decl_name']} [{entry['status']}]")
        print(f"  statement_pretty: {entry['statement_pretty']}")
        if entry.get("commit", []):
            print("  commit:")
            for line in format_commit_items(entry):
                print(f"    {line}")
        else:
            print("  commit: <empty>")
        if args.verbose:
            shard = shard_ref(key)
            print(f"  module: {entry['module']}")
            print(f"  shard: {shard['shard_id']}")
            print(f"  approval_reason: {entry.get('approval_reason', '')}")
        if args.lifecycle:
            print(f"  created_at: {entry.get('created_at', '')}")
            print(f"  updated_at: {entry.get('updated_at', '')}")
            print(f"  approved_at: {entry.get('approved_at', '')}")
            print(f"  approved_by: {entry.get('approved_by', '')}")
    if not selected:
        print("No registry entries matched the report selection.")


def print_history(args: argparse.Namespace, paths) -> None:
    records = load_history_records(paths)
    selected = select_history_records(records, decl_filter=set(args.decl), limit=args.limit)
    if not selected:
        print("No history records matched the selection.")
        return
    for record in selected:
        before_hash = None if record["before"] is None else record["before"]["statement_hash"]
        after_hash = None if record["after"] is None else record["after"]["statement_hash"]
        print(
            f"{record['timestamp']}  {record['decl_name']}  "
            f"hash {before_hash or '<none>'} -> {after_hash or '<none>'}"
        )
        if args.verbose:
            before_shard = record.get("before_shard")
            after_shard = record.get("after_shard")
            if before_shard is not None or after_shard is not None:
                print(
                    f"  shard: "
                    f"{before_shard['shard_id'] if before_shard else '<none>'} -> "
                    f"{after_shard['shard_id'] if after_shard else '<none>'}"
                )
            if record["before"] is not None:
                print(f"  before: {record['before']['statement_pretty']}")
            if record["after"] is not None:
                print(f"  after:  {record['after']['statement_pretty']}")


def regenerate_registry(args: argparse.Namespace, paths) -> None:
    generate_lean_registry(paths)
    build_registry(paths)
    print("Regenerated Lean approved statement registry artifacts.")


def audit_temporary_axioms_command(args: argparse.Namespace, paths) -> None:
    run_temporary_axiom_audit(paths, args.module)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the external approved statement registry and generated Lean artifacts."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    approve = subparsers.add_parser("approve", help="Approve declarations into a chapter/section shard.")
    approve.add_argument("--module", required=True, help="Lean module importing the declarations to probe.")
    approve.add_argument("--chapter", type=int, required=True, help="Book chapter number for the registry shard.")
    approve.add_argument("--section", type=int, required=True, help="Book section number for the registry shard.")
    approve.add_argument("--decl", action="append", required=True, help="Declaration name to approve. Repeatable.")
    approve.add_argument("--reason", help="Approval reason recorded on the current entry.")
    approve.add_argument("--author", default=DEFAULT_AUTHOR, help="Author recorded on the current entry.")

    commit = subparsers.add_parser("commit", help="Update commit comments and status on approved declarations.")
    commit.add_argument("--decl", action="append", required=True, help="Approved declaration name. Repeatable.")
    commit.add_argument("--status", choices=STATUS_CHOICES, help="Explicit review status to store.")
    commit.add_argument(
        "--message",
        help="Commit/comment message to store. Replaces the full commit list unless --append is used.",
    )
    commit.add_argument("--author", default=DEFAULT_AUTHOR, help="Author stored on commit metadata.")
    commit.add_argument("--append", action="store_true", help="Append a new commit entry instead of replacing.")
    commit.add_argument("--clear", action="store_true", help="Clear all commit entries.")
    commit.add_argument("--drop", type=int, help="Drop the given 1-based commit entry index.")

    prune = subparsers.add_parser("prune", help="Remove declarations from the approved statement registry.")
    prune.add_argument("--decl", action="append", required=True, help="Approved declaration name. Repeatable.")

    audit = subparsers.add_parser("audit", help="Probe current declarations and compare them to the registry.")
    audit.add_argument("--decl", action="append", default=[], help="Approved declaration name filter. Repeatable.")
    audit.add_argument(
        "--fail-on-status",
        choices=["needs_attention", "unreliable"],
        help="Fail if any selected declaration has status at or above this severity.",
    )

    report = subparsers.add_parser("report", help="Print current registry entries for human review.")
    report.add_argument("--decl", action="append", default=[], help="Exact declaration name filter. Repeatable.")
    report.add_argument("--status", action="append", choices=STATUS_CHOICES, default=[], help="Status filter.")
    report.add_argument(
        "--all",
        action="store_true",
        help="Print all current entries, instead of the default commit-nonempty selection.",
    )
    report.add_argument("--verbose", action="store_true", help="Include module, shard, and approval metadata.")
    report.add_argument("--lifecycle", action="store_true", help="Include lifecycle timestamps and approver.")

    history = subparsers.add_parser("history", help="Print statement-hash history records.")
    history.add_argument("--decl", action="append", default=[], help="Declaration name filter. Repeatable.")
    history.add_argument("--limit", type=int, help="Maximum number of history records to print.")
    history.add_argument("--verbose", action="store_true", help="Print shard and statement details.")

    generate = subparsers.add_parser("generate", help="Regenerate Lean registry files from current JSON data.")

    temp_audit = subparsers.add_parser(
        "audit-temporary-axioms",
        help="Generate a temporary Lean audit entry and run #assert_no_temporary_axioms.",
    )
    temp_audit.add_argument("--module", action="append", required=True, help="Lean module to import. Repeatable.")
    return parser


def main(*, project_root: Path | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args()
    if hasattr(args, "decl"):
        ensure_unique_values(args.decl, flag="--decl")
    if hasattr(args, "module") and isinstance(args.module, list):
        ensure_unique_values(args.module, flag="--module")
    root = project_root if project_root is not None else Path(__file__).resolve().parents[2]
    paths = make_paths(root.resolve())
    if args.command == "approve":
        approve_entries(args, paths)
    elif args.command == "commit":
        update_commit_and_status(args, paths)
    elif args.command == "prune":
        prune_entries(args, paths)
    elif args.command == "audit":
        audit_registry(args, paths)
    elif args.command == "report":
        report_registry(args, paths)
    elif args.command == "history":
        print_history(args, paths)
    elif args.command == "generate":
        regenerate_registry(args, paths)
    elif args.command == "audit-temporary-axioms":
        audit_temporary_axioms_command(args, paths)
    else:
        raise SystemExit(f"Unsupported command: {args.command}")
