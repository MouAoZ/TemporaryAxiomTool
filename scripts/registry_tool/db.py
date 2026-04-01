from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .common import (
    chapter_section_label,
    current_shard_filename,
    ensure_layout,
    read_json,
    sanitize_decl_token,
    shard_ref,
    write_json,
)


def normalize_commit_item(item: Any) -> dict[str, str]:
    if not isinstance(item, dict):
        raise SystemExit("Commit entries must be JSON objects.")
    return {
        "timestamp": str(item.get("timestamp", "")),
        "author": str(item.get("author", "")),
        "message": str(item.get("message", "")),
    }


def normalize_status(entry: dict[str, Any]) -> str:
    status = entry.get("status")
    if status in {"safe", "needs_attention", "unreliable"}:
        return status
    return "safe"


def normalize_commit_field(entry: dict[str, Any]) -> list[dict[str, str]]:
    commit_items = entry.get("commit", [])
    if not isinstance(commit_items, list):
        raise SystemExit(f"Entry `{entry['decl_name']}` has invalid `commit` field.")
    return [normalize_commit_item(item) for item in commit_items]


def normalize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    # 所有读路径都先走这里，手工改坏的 JSON 会在一个地方尽早暴露。
    commit_items = normalize_commit_field(entry)
    normalized = {
        "decl_name": entry["decl_name"],
        "module": entry["module"],
        "statement_pretty": entry["statement_pretty"],
        "statement_hash": str(entry["statement_hash"]),
        "status": normalize_status(entry),
        "commit": commit_items,
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
        "shard_id": chapter_section_label(chapter, section),
        "chapter": chapter,
        "section": section,
        "entries": [normalize_entry(entry) for entry in payload.get("entries", [])],
    }


def shard_key_from_payload(payload: dict[str, Any]) -> tuple[int, int]:
    return (int(payload["chapter"]), int(payload["section"]))


def default_shard_payload(chapter: int, section: int) -> dict[str, Any]:
    return {
        "shard_id": chapter_section_label(chapter, section),
        "chapter": chapter,
        "section": section,
        "entries": [],
    }


def load_current_shards(paths) -> dict[tuple[int, int], dict[str, Any]]:
    ensure_layout(paths)
    shards: dict[tuple[int, int], dict[str, Any]] = {}
    for path in sorted(paths.current_dir.glob("*.json")):
        payload = normalize_shard_payload(read_json(path))
        key = shard_key_from_payload(payload)
        shards[key] = payload
    return shards


def save_current_shards(paths, shards: dict[tuple[int, int], dict[str, Any]]) -> None:
    ensure_layout(paths)
    wanted_files: set[Path] = set()
    for key, payload in sorted(shards.items()):
        entries = sorted(
            (normalize_entry(entry) for entry in payload["entries"]),
            key=lambda item: item["decl_name"],
        )
        path = paths.current_dir / current_shard_filename(*key)
        if entries:
            wanted_files.add(path)
            write_json(
                path,
                {
                    "shard_id": chapter_section_label(*key),
                    "chapter": key[0],
                    "section": key[1],
                    "entries": entries,
                },
            )
        elif path.exists():
            path.unlink()
    # `current/` 是唯一真相来源，磁盘上不允许残留已失效 shard。
    for path in paths.current_dir.glob("*.json"):
        if path not in wanted_files:
            path.unlink()


def index_entries(shards: dict[tuple[int, int], dict[str, Any]]) -> dict[str, tuple[tuple[int, int], dict[str, Any]]]:
    index: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
    for key, payload in shards.items():
        for entry in payload["entries"]:
            decl_name = entry["decl_name"]
            if decl_name in index:
                # 重复声明会让生成 Lean registry 的运行时语义变得不明确。
                raise SystemExit(
                    f"注册库中发现重复声明 `{decl_name}`，请先修复 current shard 后再继续。"
                )
            index[decl_name] = (key, entry)
    return index


def upsert_entry(
    shards: dict[tuple[int, int], dict[str, Any]],
    key: tuple[int, int],
    entry: dict[str, Any],
) -> None:
    shard = shards.setdefault(key, default_shard_payload(*key))
    shard["entries"] = [item for item in shard["entries"] if item["decl_name"] != entry["decl_name"]]
    shard["entries"].append(normalize_entry(entry))


def remove_entry(
    shards: dict[tuple[int, int], dict[str, Any]],
    key: tuple[int, int],
    decl_name: str,
) -> None:
    shard = shards[key]
    shard["entries"] = [item for item in shard["entries"] if item["decl_name"] != decl_name]
    if not shard["entries"]:
        del shards[key]


def iter_entries(
    shards: dict[tuple[int, int], dict[str, Any]],
) -> list[tuple[tuple[int, int], dict[str, Any]]]:
    items: list[tuple[tuple[int, int], dict[str, Any]]] = []
    for key, payload in sorted(shards.items()):
        for entry in sorted(payload["entries"], key=lambda item: item["decl_name"]):
            items.append((key, entry))
    return items


def normalize_history_record(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "decl_name": payload["decl_name"],
        "timestamp": payload["timestamp"],
        "before": None if payload.get("before") is None else normalize_entry(payload["before"]),
        "after": None if payload.get("after") is None else normalize_entry(payload["after"]),
    }
    if payload.get("before_shard") is not None:
        ref = payload["before_shard"]
        normalized["before_shard"] = {
            "shard_id": ref["shard_id"],
            "chapter": int(ref["chapter"]),
            "section": int(ref["section"]),
        }
    if payload.get("after_shard") is not None:
        ref = payload["after_shard"]
        normalized["after_shard"] = {
            "shard_id": ref["shard_id"],
            "chapter": int(ref["chapter"]),
            "section": int(ref["section"]),
        }
    return normalized


def history_filename(timestamp: str, decl_name: str) -> str:
    # 带时间戳和声明名，方便人工 grep；声明名部分仍需做文件名清洗。
    compact = timestamp.replace("-", "").replace(":", "")
    return f"statement_history.{compact}.{sanitize_decl_token(decl_name)}.json"


def write_history_record(
    paths,
    *,
    timestamp: str,
    decl_name: str,
    before_key: tuple[int, int] | None,
    before: dict[str, Any] | None,
    after_key: tuple[int, int] | None,
    after: dict[str, Any] | None,
) -> None:
    payload = {
        "decl_name": decl_name,
        "timestamp": timestamp,
        "before": None if before is None else normalize_entry(before),
        "after": None if after is None else normalize_entry(after),
    }
    if before_key is not None:
        payload["before_shard"] = shard_ref(before_key)
    if after_key is not None:
        payload["after_shard"] = shard_ref(after_key)
    path = paths.history_dir / history_filename(timestamp, decl_name)
    write_json(path, payload)


def load_history_records(paths) -> list[dict[str, Any]]:
    ensure_layout(paths)
    records: list[dict[str, Any]] = []
    for path in sorted(paths.history_dir.glob("*.json"), reverse=True):
        records.append(normalize_history_record(read_json(path)))
    # 文件系统顺序不可靠，统一二次排序保证 history 输出稳定。
    records.sort(key=lambda item: (item["timestamp"], item["decl_name"]), reverse=True)
    return records


def select_history_records(
    records: list[dict[str, Any]],
    *,
    decl_filter: set[str],
    limit: int | None,
) -> list[dict[str, Any]]:
    if decl_filter:
        records = [record for record in records if record["decl_name"] in decl_filter]
    if limit is not None:
        return records[:limit]
    return records


def update_entry_commit(
    entry: dict[str, Any],
    *,
    timestamp: str,
    author: str,
    message: str | None,
    append: bool,
    clear: bool,
    drop: int | None,
) -> None:
    # commit/status 是纯人工元数据；只有 hash 变化时才由 approve 写 history。
    commit_items = copy.deepcopy(entry.get("commit", []))
    if clear:
        commit_items = []
    elif drop is not None:
        if drop < 1 or drop > len(commit_items):
            raise SystemExit(
                f"`{entry['decl_name']}` 的 commit 只有 {len(commit_items)} 条，无法删除第 {drop} 条。"
            )
        del commit_items[drop - 1]
    if message is not None:
        item = {
            "timestamp": timestamp,
            "author": author,
            "message": message,
        }
        if append:
            commit_items.append(item)
        else:
            commit_items = [item]
    entry["commit"] = commit_items
