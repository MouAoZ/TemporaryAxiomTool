#!/usr/bin/env python3
from __future__ import annotations

"""Manage the external approved statement registry and generated Lean artifacts.

The registry is maintained in two layers:

1. JSON data under `approved_statement_registry_db/`
2. generated Lean files under `TemporaryAxiomTool/ApprovedStatementRegistry/`

Lean only reads the generated modules during compilation. This script is the
offline bridge that probes declarations through the root
`TemporaryAxiomTool.ApprovedStatementRegistry` module, updates JSON history, and
regenerates the Lean registry.
"""

import argparse
import copy
import json
import re
import shlex
import subprocess
import tempfile
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn


SCHEMA_VERSION = 1
DEFAULT_AUTHOR = "ai-agent"
TOOL_NAMESPACE = "TemporaryAxiomTool"
TOOL_REGISTRY_MODULE = f"{TOOL_NAMESPACE}.ApprovedStatementRegistry"
TOOL_REGISTRY_TYPES_MODULE = f"{TOOL_REGISTRY_MODULE}.Types"
TOOL_SHARDS_MODULE = f"{TOOL_REGISTRY_MODULE}.Shards"
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
    archive_dir: Path
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
    return f"{TOOL_SHARDS_MODULE}.{lean_shard_stem(chapter, section)}"


def make_paths(project_root: Path) -> RegistryPaths:
    data_root = project_root / "approved_statement_registry_db"
    lean_root = project_root / TOOL_NAMESPACE / "ApprovedStatementRegistry"
    return RegistryPaths(
        project_root=project_root,
        data_root=data_root,
        current_dir=data_root / "current",
        history_dir=data_root / "history",
        archive_dir=data_root / "archive",
        lean_root=lean_root,
        generated_file=lean_root / "Generated.lean",
        shards_dir=lean_root / "Shards",
        build_target=TOOL_REGISTRY_MODULE,
    )


def ensure_layout(paths: RegistryPaths) -> None:
    """Create the external DB layout and Lean shard directory if missing."""
    paths.current_dir.mkdir(parents=True, exist_ok=True)
    paths.history_dir.mkdir(parents=True, exist_ok=True)
    paths.archive_dir.mkdir(parents=True, exist_ok=True)
    paths.shards_dir.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=False, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


SHARD_ID_RE = re.compile(r"^chapter_(\d+)\.section_(\d+)$")
UNKNOWN_CONSTANT_RE = re.compile(r"Unknown constant\s+([^\s:]+)")
TEMPORARY_AXIOM_TARGET_RE = re.compile(r"Invalid @\[temporary_axiom\] target\s+([^:]+):")


def render_shell_command(args: list[str]) -> str:
    return shlex.join(args)


def indent_block(text: str) -> str:
    return "\n".join(f"  {line}" if line else "  " for line in text.splitlines())


def format_guided_error(
    summary: str,
    *,
    details: str | None = None,
    evidence: list[str] | None = None,
    checks: list[str] | None = None,
    suggestions: list[str] | None = None,
    raw_output: str | None = None,
) -> str:
    # 所有预期内错误统一走这个格式，便于 agent/人工快速看到“问题-排查-建议”三层信息。
    parts = [f"错误: {summary}"]
    if details:
        parts.extend(["", f"说明: {details}"])
    if evidence:
        parts.extend(["", "详情:"])
        parts.extend(f"- {item}" for item in evidence)
    if checks:
        parts.extend(["", "排查:"])
        parts.extend(f"- {item}" for item in checks)
    if suggestions:
        parts.extend(["", "建议:"])
        parts.extend(f"- {item}" for item in suggestions)
    if raw_output:
        parts.extend(["", "原始输出:", indent_block(raw_output)])
    return "\n".join(parts)


def fail(
    summary: str,
    *,
    details: str | None = None,
    evidence: list[str] | None = None,
    checks: list[str] | None = None,
    suggestions: list[str] | None = None,
    raw_output: str | None = None,
) -> NoReturn:
    raise SystemExit(
        format_guided_error(
            summary,
            details=details,
            evidence=evidence,
            checks=checks,
            suggestions=suggestions,
            raw_output=raw_output,
        )
    )


def fail_missing_registry_decl(decl_name: str, *, action: str) -> NoReturn:
    fail(
        f"`{action}` 无法继续，因为 `{decl_name}` 不在已批准陈述注册库中",
        details="当前 `approved_statement_registry_db/current/` 快照里没有这条记录。",
        checks=[
            f"确认 `{decl_name}` 是否已经通过 `approve` 写入注册库",
            f"运行 `python3 scripts/manage_approved_statement_registry.py history --decl {decl_name} --include-archive` 查看它是否曾被 prune、rollback 或归档",
            "如果定理刚改名，确认这里使用的是最新的 Lean 全限定名",
        ],
        suggestions=[
            "如果这是一个新批准的定理，先运行 `approve` 再执行当前命令",
            "如果你原本预期它已经存在，先检查 current/history/archive 三层数据是否一致",
        ],
    )


def fail_history_event_not_found(event_id: str) -> NoReturn:
    fail(
        f"找不到 history 事件 `{event_id}`",
        details="脚本已经同时检查了 live history 和 archive 归档包，但没有找到这个事件编号。",
        checks=[
            "确认 `--event-id` 没有截断或手误",
            f"运行 `python3 scripts/manage_approved_statement_registry.py history --include-archive --decl <DECL_NAME>` 缩小范围后再查",
            "如果只记得最近事件，可以先运行 `python3 scripts/manage_approved_statement_registry.py history --include-archive --limit 50`",
        ],
        suggestions=[
            "从 `history` 输出里复制完整 event id 后再执行 `rollback`",
            "如果事件已经被归档，不需要手工恢复，`rollback` 会自动从 `archive/` 中读取",
        ],
    )


def fail_archive_usage(problem: str) -> NoReturn:
    fail(
        "history 归档参数组合不合法",
        details=problem,
        checks=[
            "`--archive` 模式只接受归档选择参数，不支持普通 history 浏览参数混用",
            "选择范围时应二选一：`--archive-all` 或一个/多个 `--decl`",
            "只想预览时保留 `--archive` 但不要加 `--execute`",
        ],
        suggestions=[
            "查看 `python3 scripts/manage_approved_statement_registry.py history --help` 确认参数组合",
            "如果只是想查看历史，不要传 `--archive`",
        ],
    )


def explain_command_failure(
    args: list[str],
    description: str,
    returncode: int,
    output: str,
) -> dict[str, Any]:
    # 这里集中识别最常见的 Lean 失败模式；识别不到时再退回通用报错模板。
    command_text = render_shell_command(args)
    checks = [
        f"失败命令: {command_text}",
        f"退出码: {returncode}",
    ]
    generic = {
        "summary": f"{description} 失败",
        "details": "外部命令返回了非零退出码。",
        "checks": checks + [
            "从原始输出中定位第一条 Lean 报错信息",
            "确认当前分支上的 Lean 文件和生成文件没有处于半更新状态",
        ],
        "suggestions": [
            "必要时先执行 `lake build`，确认基础构建是干净的",
            "根据原始输出里的首个错误位置回到对应 Lean 文件排查",
        ],
        "raw_output": output,
    }

    if match := UNKNOWN_CONSTANT_RE.search(output):
        missing_decl = match.group(1).strip("`")
        module_hint = description.removeprefix("Probe module ") if description.startswith("Probe module ") else None
        probe_checks = checks + [
            f"确认 `--decl` 使用的是 Lean 全限定名，例如 `{missing_decl}`",
            "确认对应声明确实存在，并且没有被最近的重命名或 namespace 调整影响",
        ]
        if module_hint is not None:
            probe_checks.append(f"确认 `--module {module_hint}` 可以直接导入该声明")
        return {
            "summary": "Lean 无法找到要处理的声明",
            "details": f"{description} 时出现 `Unknown constant {missing_decl}`。",
            "checks": probe_checks,
            "suggestions": [
                "先在对应 Lean 文件中用 `#check <DECL_NAME>` 验证声明名是否正确",
                "修正 `--module` 或 `--decl` 后重新执行当前命令",
            ],
            "raw_output": output,
        }

    if "Invalid @[temporary_axiom] target" in output:
        target_match = TEMPORARY_AXIOM_TARGET_RE.search(output)
        target_decl = target_match.group(1).strip("`") if target_match else "该声明"
        if "not present in the approved statement registry" in output:
            return {
                "summary": "`@[temporary_axiom]` 指向了未批准的定理",
                "details": f"{target_decl} 被标记为 `@[temporary_axiom]`，但它不在已批准陈述注册库中。",
                "checks": checks + [
                    f"先确认 `{target_decl}` 是否已经通过 `approve` 进入注册库",
                    "确认当前 build 使用的是最新生成的 registry Lean 文件",
                    "确认声明名没有因为 namespace 或 private/protected 变化而改变",
                ],
                "suggestions": [
                    "如果该定理允许跳过，先运行 `approve` 预批准其陈述",
                    "如果这条标签是误加的，直接删除 `@[temporary_axiom]` 后重试",
                ],
                "raw_output": output,
            }
        if "does not match the elaborated statement" in output:
            return {
                "summary": "`@[temporary_axiom]` 的陈述与已批准记录不一致",
                "details": f"{target_decl} 当前 elaborated statement 的 hash 与注册库记录不一致。",
                "checks": checks + [
                    "确认最近是否修改了 theorem statement、binder、implicit 参数或 namespace",
                    "确认当前批准记录对应的是最新版本的陈述，而不是旧分支遗留数据",
                    "如需人工复核，可先查看 current shard 中保存的 `statement_pretty`",
                ],
                "suggestions": [
                    "如果新陈述是正确的，重新运行 `approve` 更新注册记录",
                    "如果陈述不应变化，回查最近对该 theorem 头部的修改",
                ],
                "raw_output": output,
            }

    if "object file" in output and "does not exist" in output:
        return {
            "summary": "Lean 构建产物缺失",
            "details": f"{description} 依赖的 `.olean` 文件不存在，通常表示依赖模块尚未成功构建。",
            "checks": checks + [
                "确认相关模块可以独立 `lake build` 通过",
                "确认 import 路径没有拼写错误",
            ],
            "suggestions": [
                f"先运行 `lake build` 或 `lake build {TOOL_REGISTRY_MODULE}`",
                "如果刚改过生成文件，先执行 `generate` 或重新运行触发生成的命令",
            ],
            "raw_output": output,
        }

    if "unknown module prefix" in output or "unknown package" in output:
        return {
            "summary": "Lean 无法解析模块导入路径",
            "details": f"{description} 时遇到了模块路径错误。",
            "checks": checks + [
                "确认 `--module` 对应的是合法的 Lean 模块名，而不是文件路径",
                "确认项目根目录下的 `lakefile` / `lean-toolchain` 与当前源码匹配",
            ],
            "suggestions": [
                "修正模块名后重试",
                "先运行 `lake build` 观察是否存在更早的导入错误",
            ],
            "raw_output": output,
        }

    return generic


def parse_shard_id(shard_id: str) -> tuple[int, int]:
    match = SHARD_ID_RE.match(shard_id)
    if match is None:
        fail(
            f"无效的 shard id: `{shard_id}`",
            details="当前数据不符合 `chapter_<CC>.section_<SS>` 命名约定。",
            checks=[
                "确认外部数据库 JSON 没有被手工改坏",
                "确认生成脚本没有写入自定义分片名",
            ],
            suggestions=[
                "优先通过管理脚本重建 current/history 数据，而不是手工编辑 shard id",
            ],
        )
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
    # current/history/archive 都通过同一个 entry 规范化器收敛字段顺序与默认值。
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
    # history 里的 before/after 需要兼容旧格式，因此这里补齐 shard 定位信息。
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


def normalize_archive_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    # archive 文件本质上是 history 事件包，因此仍复用 history 的规范化逻辑。
    events = [normalize_history_event(event) for event in bundle.get("events", [])]
    normalized = {
        "schema_version": int(bundle.get("schema_version", SCHEMA_VERSION)),
        "archive_id": bundle["archive_id"],
        "created_at": bundle["created_at"],
        "author": bundle["author"],
        "reason": bundle["reason"],
        "mode": bundle["mode"],
        "source_event_count": int(bundle.get("source_event_count", len(events))),
        "source_event_ids": list(bundle.get("source_event_ids", [event["event_id"] for event in events])),
        "events": events,
    }
    if "decl_filter" in bundle:
        normalized["decl_filter"] = list(bundle["decl_filter"])
    return normalized


def archive_bundle_filename(archive_id: str) -> str:
    return f"history_archive.{archive_id}.json"


def load_live_history_records(paths: RegistryPaths) -> list[dict[str, Any]]:
    # live history 保留最近仍在主历史目录中的事件文件。
    records: list[dict[str, Any]] = []
    for path in sorted(paths.history_dir.glob("*.json"), reverse=True):
        records.append(
            {
                "source": "live",
                "path": path,
                "event": normalize_history_event(read_json(path)),
            }
        )
    return records


def load_archive_records(paths: RegistryPaths) -> list[dict[str, Any]]:
    # archive 里存放的是归档包，因此需要先展开 bundle，再按 event 维度返回。
    records: list[dict[str, Any]] = []
    for path in sorted(paths.archive_dir.glob("*.json"), reverse=True):
        bundle = normalize_archive_bundle(read_json(path))
        for event in bundle["events"]:
            records.append(
                {
                    "source": "archive",
                    "path": path,
                    "archive_id": bundle["archive_id"],
                    "event": event,
                }
            )
    records.sort(key=lambda record: record["event"]["event_id"], reverse=True)
    return records


def find_history_event(paths: RegistryPaths, event_id: str) -> tuple[dict[str, Any], str]:
    live_path = paths.history_dir / f"{event_id}.json"
    if live_path.exists():
        return normalize_history_event(read_json(live_path)), "live"
    # rollback 需要对 live 与 archive 做统一查询，避免用户手工恢复旧事件。
    for path in sorted(paths.archive_dir.glob("*.json"), reverse=True):
        bundle = normalize_archive_bundle(read_json(path))
        for event in bundle["events"]:
            if event["event_id"] == event_id:
                return event, f"archive:{bundle['archive_id']}"
    fail_history_event_not_found(event_id)


def event_matches_decl_filter(event: dict[str, Any], decl_filter: set[str]) -> bool:
    if not decl_filter:
        return True
    return any(change["decl_name"] in decl_filter for change in event["changes"])


def archive_history(args: argparse.Namespace, paths: RegistryPaths) -> None:
    decl_filter = set(args.decl)
    if args.limit is not None:
        fail_archive_usage("`--archive` 模式不支持 `--limit`。")
    if args.include_archive or args.archive_only:
        fail_archive_usage("`--archive` 不能和 `--include-archive` 或 `--archive-only` 同时使用。")
    if args.archive_all and decl_filter:
        fail_archive_usage("请在 `--archive-all` 和 `--decl` 之间二选一，不要同时传入。")
    if not args.archive_all and not decl_filter:
        fail_archive_usage("执行归档时，必须提供至少一个 `--decl` 或直接使用 `--archive-all`。")

    selected_records = []
    for record in load_live_history_records(paths):
        if args.archive_all or event_matches_decl_filter(record["event"], decl_filter):
            selected_records.append(record)

    if not selected_records:
        print("No live history events matched the archive selection.")
        return

    archive_id = make_event_id("history_archive")
    bundle_path = paths.archive_dir / archive_bundle_filename(archive_id)
    archive_reason = args.reason
    if archive_reason is None:
        if args.archive_all:
            archive_reason = "archive all live history events"
        else:
            archive_reason = "archive live history events for selected declarations"

    bundle = {
        "schema_version": SCHEMA_VERSION,
        "archive_id": archive_id,
        "created_at": now_utc_iso(),
        "author": args.author,
        "reason": archive_reason,
        "mode": "all" if args.archive_all else "decl_filter",
        "source_event_count": len(selected_records),
        "source_event_ids": [record["event"]["event_id"] for record in selected_records],
        "events": [record["event"] for record in selected_records],
    }
    if not args.archive_all:
        bundle["decl_filter"] = sorted(decl_filter)

    prefix = "Would archive" if not args.execute else "Archived"
    print(f"{prefix} {len(selected_records)} live history event(s) into {bundle_path.relative_to(paths.project_root)}")
    for record in selected_records:
        event = record["event"]
        print(
            f"- {event['event_id']} action={event['action']} "
            f"timestamp={event['timestamp']} reason={event['reason']}"
        )

    if not args.execute:
        print("Dry run only. Re-run with --execute to write the archive bundle and remove live history files.")
        return

    # 先写 archive bundle，再删除 live history，可确保压缩历史时不会直接丢数据。
    write_json(bundle_path, normalize_archive_bundle(bundle))
    for record in selected_records:
        record["path"].unlink()
    print("Removed archived event files from approved_statement_registry_db/history/.")


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
        # current/ 是“当前真相”，读入时统一做规范化，避免历史格式差异传播到后续步骤。
        payload = normalize_shard_payload(read_json(path))
        key = shard_key_from_payload(payload)
        shards[key] = payload
    return shards


def save_current_shards(paths: RegistryPaths, shards: dict[tuple[int, int], dict[str, Any]]) -> None:
    """Rewrite current JSON shards and delete empty shard files."""
    ensure_layout(paths)
    wanted_files: set[Path] = set()
    # 重新整体落盘 current/，可以保证字段顺序、排序与空分片清理逻辑一致。
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
                fail(
                    f"注册库中发现重复声明 `{decl_name}`",
                    details="同一个定理同时出现在多个 current shard 中，这会破坏回滚与审计语义。",
                    checks=[
                        "检查 `approved_statement_registry_db/current/` 下是否有 merge conflict 后遗留的重复条目",
                        "确认没有手工复制或手工移动 entry",
                    ],
                    suggestions=[
                        "优先保留正确的 shard 记录后重新运行 `generate`",
                        "如需追溯来源，可先运行 `history --decl <DECL_NAME> --include-archive`",
                    ],
                )
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
    # 所有外部命令统一走这里，便于集中补充 Lean/脚本常见失败的诊断信息。
    try:
        result = subprocess.run(
            args,
            cwd=paths.project_root,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        fail(
            f"{description} 无法启动",
            details=f"系统中找不到命令 `{args[0]}`。",
            checks=[
                f"确认 `{args[0]}` 已安装并在 PATH 中",
                "确认当前终端环境和项目平时使用的是同一套工具链",
            ],
            suggestions=[
                "先在终端单独执行该命令确认可用",
                "如果是 Lean 环境问题，优先检查 `elan` / `lake` 是否可正常调用",
            ],
        )
    if result.returncode != 0:
        output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
        explanation = explain_command_failure(args, description, result.returncode, output or "(no stdout/stderr captured)")
        fail(
            explanation["summary"],
            details=explanation["details"],
            checks=explanation["checks"],
            suggestions=explanation["suggestions"],
            raw_output=explanation["raw_output"],
        )
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
        fail(
            f"Probe 模块 `{module_name}` 返回了不完整的结果",
            details=f"期望拿到 {len(decl_names)} 条声明探测结果，实际只解析出 {len(payloads)} 条。",
            checks=[
                "确认探测输出中每个目标定理都生成了一行 JSON",
                "确认对应 Lean 文件没有额外的报错或中断信息混入输出",
                "如果这批声明是刚新增的，先执行一次 `lake build` 再重试",
            ],
            suggestions=[
                "检查下方原始输出中缺失的是哪一条声明",
                "必要时改成一次只 probe 一个声明，缩小问题范围",
            ],
            raw_output=result.stdout.strip(),
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
        # 每个 chapter/section shard 生成一个独立 Lean 模块，便于冲突定位与增量合并。
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
            f"import {TOOL_REGISTRY_TYPES_MODULE}\n\n"
            f"namespace {TOOL_REGISTRY_MODULE}\n\n"
            f"def {const_name} : Array ApprovedStatement := #[\n"
            f"{entries_block}\n"
            "]\n\n"
            f"end {TOOL_REGISTRY_MODULE}\n"
        )
        shard_path.write_text(shard_source, encoding="utf-8")

    for path in paths.shards_dir.glob("*.lean"):
        if path not in wanted_files:
            path.unlink()

    import_block = "\n".join([f"import {TOOL_REGISTRY_TYPES_MODULE}", *imports])
    if consts:
        body = " ++\n  ".join(consts)
        generated_source = (
            "/- Auto-generated registry aggregate. Do not edit by hand. -/\n"
            f"{import_block}\n\n"
            f"namespace {TOOL_REGISTRY_MODULE}\n\n"
            "def generatedApprovedStatements : Array ApprovedStatement :=\n"
            f"  {body}\n\n"
            f"end {TOOL_REGISTRY_MODULE}\n"
        )
    else:
        generated_source = (
            "/- Auto-generated registry aggregate. Do not edit by hand. -/\n"
            f"import {TOOL_REGISTRY_TYPES_MODULE}\n\n"
            f"namespace {TOOL_REGISTRY_MODULE}\n\n"
            "def generatedApprovedStatements : Array ApprovedStatement := #[]\n\n"
            f"end {TOOL_REGISTRY_MODULE}\n"
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
        # 若同一定理被重新 approve，保留既有 review notes，避免人工审计上下文丢失。
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
            fail_missing_registry_decl(decl_name, action="commit")
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
            fail_missing_registry_decl(decl_name, action="prune")
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
    target_event, event_source = find_history_event(paths, args.event_id)
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
        # rollback 是对历史事件做反向重放，因此必须保留 before/after 与 shard 定位信息。
        current_index = index_entries(shards)
        current_before = copy.deepcopy(current_index[decl_name][1]) if decl_name in current_index else None
        current_before_key = current_index[decl_name][0] if decl_name in current_index else None

        if after is None and before is not None:
            if before_shard is None:
                fail(
                    f"`rollback` 缺少 `{decl_name}` 的 before_shard 元数据",
                    details="历史事件不能唯一定位该定理在回滚前所属的 chapter/section 分片。",
                    checks=[
                        "检查对应 history 或 archive 事件文件是否被手工改动",
                        "确认该事件是否来自旧格式数据且未被正确规范化",
                    ],
                    suggestions=[
                        f"先用 `history --include-archive` 找到 `{args.event_id}` 对应的原始记录并检查 `changes` 字段",
                    ],
                )
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
                fail(
                    f"`rollback` 缺少 `{decl_name}` 的 before_shard 元数据",
                    details="历史事件不能唯一定位该定理在回滚前所属的 chapter/section 分片。",
                    checks=[
                        "检查对应 history 或 archive 事件文件是否被手工改动",
                        "确认该事件是否来自旧格式数据且未被正确规范化",
                    ],
                    suggestions=[
                        f"先用 `history --include-archive` 找到 `{args.event_id}` 对应的原始记录并检查 `changes` 字段",
                    ],
                )
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
    print(f"Rolled back history event {args.event_id} from {event_source}.")


def audit_registry(args: argparse.Namespace, paths: RegistryPaths) -> None:
    """Compare stored hashes to freshly probed declarations and report review notes."""
    run_command(paths, ["lake", "build", paths.build_target], f"Build {paths.build_target}")
    shards = load_current_shards(paths)
    index = index_entries(shards)
    selected_names = args.decl or sorted(index.keys())
    entries: list[dict[str, Any]] = []
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
        fail(
            "approved statement registry 审计失败",
            details="当前 Lean 环境下的 elaborated statement hash 与注册库记录不一致。",
            evidence=problems,
            checks=[
                "确认这些定理最近是否改动了 theorem statement、参数顺序、隐式参数或 namespace",
                "确认当前分支上的 generated registry 与 JSON 数据是同步生成的",
            ],
            suggestions=[
                "如果当前陈述是正确的，请重新运行 `approve` 更新这些定理的批准记录",
                "如果陈述不应变化，请回查对应 theorem 头部的最近修改",
            ],
        )

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
            fail(
                "approved statement registry 审计因 review 状态触发失败",
                details=f"以下定理的 `review_status` 已达到或超过 `{args.fail_on_review_status}`。",
                evidence=blockers,
                checks=[
                    "检查这些定理最近的 review note，确认是否仍需要人工复核",
                    "如果问题已解决，可重新 `approve` 或追加新的 review commit 更新状态",
                ],
                suggestions=[
                    "先运行 `history --decl <DECL_NAME> --include-archive` 查看相关操作轨迹",
                    "人工审核完成前，不要把这些定理视为完全可信的跳过对象",
                ],
            )

    print(f"Approved statement registry audit passed for {len(entries)} declaration(s).")


def print_history(args: argparse.Namespace, paths: RegistryPaths) -> None:
    """Print recent registry events, including newly added review notes."""
    if args.archive:
        archive_history(args, paths)
        return

    decl_filter = set(args.decl)
    events = []
    if not args.archive_only:
        for record in load_live_history_records(paths):
            event = record["event"]
            if event_matches_decl_filter(event, decl_filter):
                events.append({"event": event, "source": "live"})
    if args.include_archive or args.archive_only:
        for record in load_archive_records(paths):
            event = record["event"]
            if event_matches_decl_filter(event, decl_filter):
                events.append({"event": event, "source": "archive", "archive_id": record["archive_id"]})
    # 无论来源于 live 还是 archive，最终都按 event_id 的时间顺序统一展示。
    events.sort(key=lambda record: record["event"]["event_id"], reverse=True)
    if args.limit is not None:
        events = events[: args.limit]
    for record in events:
        event = record["event"]
        source_suffix = ""
        if record["source"] == "archive":
            source_suffix = f"  source=archive:{record['archive_id']}"
        print(
            f"{event['event_id']}  action={event['action']}  author={event['author']}  "
            f"timestamp={event['timestamp']}  reason={event['reason']}{source_suffix}"
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
    history.add_argument(
        "--include-archive",
        action="store_true",
        help="Include archived history bundles when printing history.",
    )
    history.add_argument(
        "--archive-only",
        action="store_true",
        help="Print only archived history bundles.",
    )
    history.add_argument(
        "--archive",
        action="store_true",
        help="Archive matching live history events into approved_statement_registry_db/archive/.",
    )
    history.add_argument(
        "--archive-all",
        action="store_true",
        help="With --archive, archive all live history events instead of selecting by --decl.",
    )
    history.add_argument(
        "--reason",
        help="Archive reason recorded in the archive bundle metadata when using --archive.",
    )
    history.add_argument(
        "--author",
        default=DEFAULT_AUTHOR,
        help="Archive author recorded in archive bundle metadata when using --archive.",
    )
    history.add_argument(
        "--execute",
        action="store_true",
        help="Apply the history archive operation. Without this flag, --archive performs a dry run.",
    )

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
