from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_AUTHOR = "ai-agent"
TOOL_NAMESPACE = "TemporaryAxiomTool"
TOOL_REGISTRY_MODULE = f"{TOOL_NAMESPACE}.ApprovedStatementRegistry"
TOOL_REGISTRY_TYPES_MODULE = f"{TOOL_REGISTRY_MODULE}.Types"
TOOL_SHARDS_MODULE = f"{TOOL_REGISTRY_MODULE}.Shards"
STATUS_ORDER = {
    "safe": 0,
    "needs_attention": 1,
    "unreliable": 2,
}
STATUS_CHOICES = tuple(STATUS_ORDER)
DECL_TOKEN_RE = re.compile(r"[^A-Za-z0-9]+")


@dataclass(frozen=True)
class RegistryPaths:
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
        lean_root=lean_root,
        generated_file=lean_root / "Generated.lean",
        shards_dir=lean_root / "Shards",
        build_target=TOOL_REGISTRY_MODULE,
    )


def ensure_layout(paths: RegistryPaths) -> None:
    paths.current_dir.mkdir(parents=True, exist_ok=True)
    paths.history_dir.mkdir(parents=True, exist_ok=True)
    paths.shards_dir.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=False, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def shard_ref(key: tuple[int, int]) -> dict[str, Any]:
    chapter, section = key
    return {
        "shard_id": chapter_section_label(chapter, section),
        "chapter": chapter,
        "section": section,
    }


def status_meets_threshold(actual: str, threshold: str) -> bool:
    return STATUS_ORDER[actual] >= STATUS_ORDER[threshold]


def sanitize_decl_token(decl_name: str) -> str:
    token = DECL_TOKEN_RE.sub("_", decl_name).strip("_")
    return token or "decl"
