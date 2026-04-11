from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .common import (
    IMPORT_RE,
    TOOL_ROOT_MODULE,
    TOOL_TEMPORARY_AXIOM_MODULE,
    TOOL_THEOREM_REGISTRY_MODULE,
    TOOL_THEOREM_REGISTRY_SHARDS_MODULE_PREFIX,
    acquire_prepare_lock,
    ensure_layout,
    fail,
    is_host_project_module,
    make_paths,
    module_name_to_path,
    module_name_to_relative_path,
    path_to_module_name,
    read_json,
    release_prepare_lock,
    write_json,
)
from .lean_ops import (
    build_module,
    compile_importable_lean_module,
    compute_text_file_hashes,
    ensure_probe_tool_ready,
    generated_shard_module_source,
    generated_shard_runtime_module_name,
    lean_string_literal,
    module_artifact_path,
    run_command,
    run_lean_module_file,
    run_lean_module_source,
    run_lean_probe,
    try_git_head,
    write_active_shards,
    write_collect_shards,
    write_inactive_shards,
)


SORRY_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_'])sorry(?![A-Za-z0-9_'])")
THEOREM_LIKE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_'])(theorem|lemma)(?![A-Za-z0-9_'])")
LEAN_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_']*")
INLINE_MODULE_LIST_LIMIT = 10
INLINE_PERMITTED_AXIOM_LIMIT = 12
DECL_MODIFIER_TOKENS = {
    "private",
    "protected",
    "public",
    "noncomputable",
    "unsafe",
    "partial",
    "local",
    "scoped",
}
REGISTRY_SCHEMA_VERSION = 2
SESSION_SCHEMA_VERSION = 4
MAX_PROBE_WORKERS = 4
COLLECT_PAYLOAD_KIND = "temporary_axiom_collect"


@dataclass(frozen=True)
class TargetSpec:
    module_name: str | None
    decl_name: str
    decl_is_short_name: bool


@dataclass(frozen=True)
class ArtifactIssue:
    module_name: str
    detail: str


def relative_path_str(project_root: Path, path: Path) -> str:
    return path.resolve().relative_to(project_root.resolve()).as_posix()


def normalize_range(raw: list[object]) -> dict[str, int]:
    return {
        "line": int(raw[0]) + 1,
        "column": int(raw[1]),
        "end_line": int(raw[2]) + 1,
        "end_column": int(raw[3]),
        "selection_line": int(raw[4]) + 1 if len(raw) > 4 else int(raw[0]) + 1,
        "selection_column": int(raw[5]) if len(raw) > 5 else int(raw[1]),
    }


def parse_target_spec(target_spec: str) -> TargetSpec:
    module_name, sep, decl_part = target_spec.partition(":")
    if sep:
        module_name = module_name.strip()
        decl_part = decl_part.strip()
        if not module_name or not decl_part or ":" in decl_part:
            fail(
                "target 参数格式无效。",
                details=[f"收到：`{target_spec}`"],
                hints=[
                    "使用 `--target <module>:<decl>` 或 `--target <fully-qualified-decl>`。",
                    "例如：`--target MyProj.Section:goal`、`--target MyProj.Section:My.Namespace.goal`、`--target MyProj.Section.goal`。",
                ],
            )
        return TargetSpec(
            module_name=module_name,
            decl_name=decl_part,
            decl_is_short_name="." not in decl_part,
        )
    decl_name = target_spec.strip()
    if not decl_name or ":" in decl_name or "." not in decl_name:
        fail(
            "target 参数格式无效。",
            details=[f"收到：`{target_spec}`"],
            hints=[
                "使用 `--target <module>:<decl>` 或 `--target <fully-qualified-decl>`。",
                "例如：`--target MyProj.Section:goal`、`--target MyProj.Section:My.Namespace.goal`、`--target MyProj.Section.goal`。",
            ],
        )
    return TargetSpec(
        module_name=None,
        decl_name=decl_name,
        decl_is_short_name=False,
    )


def candidate_target_modules_from_decl_name(paths, decl_name: str) -> list[str]:
    parts = decl_name.split(".")
    candidates: list[str] = []
    for end in range(len(parts) - 1, 0, -1):
        module_name = ".".join(parts[:end])
        if is_session_host_module(paths, module_name) and module_name not in candidates:
            candidates.append(module_name)
    return candidates


def range_key(payload: dict[str, object]) -> tuple[int, int]:
    range_info = payload["range"]
    assert isinstance(range_info, dict)
    return (int(range_info["line"]), int(range_info["column"]))


def short_decl_name(decl_name: str) -> str:
    return decl_name.rsplit(".", 1)[-1]


def is_internal_tool_support_module(module_name: str) -> bool:
    return (
        module_name == TOOL_ROOT_MODULE
        or module_name == TOOL_TEMPORARY_AXIOM_MODULE
        or module_name == TOOL_THEOREM_REGISTRY_MODULE
        or module_name.startswith(TOOL_THEOREM_REGISTRY_MODULE + ".")
        or module_name.startswith(TOOL_THEOREM_REGISTRY_SHARDS_MODULE_PREFIX + ".")
        or module_name == f"{TOOL_ROOT_MODULE}.StatementHash"
    )


def is_session_host_module(paths, module_name: str) -> bool:
    return is_host_project_module(paths.project_root, module_name) and not is_internal_tool_support_module(module_name)


def user_visible_decl_name(module_name: str, decl_name: str) -> str:
    if not decl_name.startswith("_private."):
        return decl_name
    parts = decl_name.split(".")
    module_parts = module_name.split(".")
    prefix = ["_private", *module_parts]
    if len(parts) <= len(prefix) + 1:
        return decl_name
    if parts[: len(prefix)] != prefix:
        return decl_name
    if not parts[len(prefix)].isdigit():
        return decl_name
    suffix = parts[len(prefix) + 1 :]
    if not suffix:
        return decl_name
    return ".".join(suffix)


def entry_probe_decl_name(entry: dict[str, object]) -> str:
    implementation_name = entry.get("implementation_name")
    if implementation_name:
        return str(implementation_name)
    return str(entry["decl_name"])


def normalize_probed_decl(
    payload: dict[str, object],
    *,
    module_name: str,
    relative_file: str,
    range_info: dict[str, int],
) -> dict[str, object]:
    decl_name = str(payload["decl_name"])
    return {
        "decl_name": decl_name,
        "module": module_name,
        "file": relative_file,
        "statement_hash": str(payload["statement_hash"]),
        "range": range_info,
    }


def parse_json_payload_lines(*texts: str) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for text in texts:
        for line in text.splitlines():
            stripped = line.strip()
            start = stripped.find("{")
            end = stripped.rfind("}")
            if start == -1 or end == -1 or end < start:
                continue
            candidate = stripped[start : end + 1]
            try:
                payload = read_json_payload_line(candidate)
            except ValueError:
                continue
            payloads.append(payload)
    return payloads


def read_json_payload_line(line: str) -> dict[str, object]:
    try:
        payload = json.loads(line)
    except Exception as exc:  # pragma: no cover - helper only filters malformed log lines
        raise ValueError from exc
    if not isinstance(payload, dict):
        raise ValueError
    return payload


def normalize_collected_decl(paths, payload: dict[str, object]) -> dict[str, object]:
    module_name = str(payload["module"])
    return {
        "decl_name": str(payload["decl_name"]),
        "module": module_name,
        "file": relative_path_str(paths.project_root, module_name_to_path(paths.project_root, module_name)),
        "statement_hash": str(payload["statement_hash"]),
        "explicit_sorry": bool(payload.get("explicit_sorry", False)),
        "ordinal": int(str(payload.get("ordinal", "0"))),
    }


def ilean_path_for_module(paths, module_name: str) -> Path:
    return paths.lean_build_lib_root / module_name_to_relative_path(module_name).with_suffix(".ilean")


def olean_path_for_module(paths, module_name: str) -> Path:
    return module_artifact_path(paths, module_name, ".olean")


def trace_path_for_module(paths, module_name: str) -> Path:
    return module_artifact_path(paths, module_name, ".trace")


@lru_cache(maxsize=None)
def load_ilean_metadata(paths, module_name: str) -> dict[str, Any]:
    path = ilean_path_for_module(paths, module_name)
    if not path.exists():
        fail(
            "缺少模块的 `.ilean` 元数据。",
            details=[
                f"模块：`{module_name}`",
                f"期望路径：`{relative_path_str(paths.project_root, path)}`",
            ],
            hints=[f"先确认模块可以单独构建：`lake build {module_name}`。"],
        )
    return read_json(path)


def build_line_starts(text: str) -> list[int]:
    starts = [0]
    for idx, char in enumerate(text):
        if char == "\n":
            starts.append(idx + 1)
    return starts


def offset_for_line_column(text: str, line_starts: list[int], line: int, column: int) -> int:
    if line <= 0:
        return 0
    line_idx = line - 1
    if line_idx >= len(line_starts):
        return len(text)
    start = line_starts[line_idx]
    next_start = line_starts[line_idx + 1] if line_idx + 1 < len(line_starts) else len(text)
    line_text = text[start:next_start]
    if line_text.endswith("\n"):
        line_text = line_text[:-1]
    safe_column = max(0, min(column, len(line_text)))
    return start + safe_column


def slice_command_text_for_decl(text: str, line_starts: list[int], range_info: dict[str, int]) -> str:
    start = offset_for_line_column(text, line_starts, int(range_info["line"]), int(range_info["column"]))
    end = offset_for_line_column(text, line_starts, int(range_info["end_line"]), int(range_info["end_column"]))
    if end < start:
        end = start
    return text[start:end]


def strip_lean_comments_and_strings(text: str) -> str:
    out: list[str] = []
    idx = 0
    block_depth = 0
    in_line_comment = False
    in_string = False
    while idx < len(text):
        char = text[idx]
        nxt = text[idx + 1] if idx + 1 < len(text) else ""
        if block_depth > 0:
            if char == "/" and nxt == "-":
                block_depth += 1
                out.extend([" ", " "])
                idx += 2
                continue
            if char == "-" and nxt == "/":
                block_depth -= 1
                out.extend([" ", " "])
                idx += 2
                continue
            out.append("\n" if char == "\n" else " ")
            idx += 1
            continue
        if in_line_comment:
            if char == "\n":
                in_line_comment = False
                out.append("\n")
            else:
                out.append(" ")
            idx += 1
            continue
        if in_string:
            if char == "\\" and nxt:
                out.extend([" ", " "])
                idx += 2
                continue
            if char == '"':
                in_string = False
                out.append(" ")
            else:
                out.append("\n" if char == "\n" else " ")
            idx += 1
            continue
        if char == "-" and nxt == "-":
            in_line_comment = True
            out.extend([" ", " "])
            idx += 2
            continue
        if char == "/" and nxt == "-":
            block_depth = 1
            out.extend([" ", " "])
            idx += 2
            continue
        if char == '"':
            in_string = True
            out.append(" ")
            idx += 1
            continue
        out.append(char)
        idx += 1
    return "".join(out)


def decl_command_keyword_from_sanitized(sanitized: str) -> str | None:
    idx = 0
    while idx < len(sanitized):
        while idx < len(sanitized) and sanitized[idx].isspace():
            idx += 1
        if idx >= len(sanitized):
            return None
        if sanitized.startswith("@[", idx):
            idx += 2
            bracket_depth = 1
            while idx < len(sanitized) and bracket_depth > 0:
                char = sanitized[idx]
                if char == "[":
                    bracket_depth += 1
                elif char == "]":
                    bracket_depth -= 1
                idx += 1
            continue
        match = LEAN_IDENT_RE.match(sanitized, idx)
        if match is None:
            return None
        token = match.group(0)
        idx = match.end()
        if token in DECL_MODIFIER_TOKENS:
            continue
        return token
    return None


def decl_is_explicit_sorry_theorem_like_sanitized(sanitized_snippet: str) -> bool:
    if "sorry" not in sanitized_snippet:
        return False
    token = decl_command_keyword_from_sanitized(sanitized_snippet)
    if token not in {"theorem", "lemma"}:
        return False
    return SORRY_TOKEN_RE.search(sanitized_snippet) is not None


def decl_is_proved_theorem_like_sanitized(sanitized_snippet: str) -> bool:
    token = decl_command_keyword_from_sanitized(sanitized_snippet)
    if token not in {"theorem", "lemma"}:
        return False
    return SORRY_TOKEN_RE.search(sanitized_snippet) is None


def module_may_contain_theorem_like_sanitized(sanitized_text: str) -> bool:
    return THEOREM_LIKE_TOKEN_RE.search(sanitized_text) is not None


@lru_cache(maxsize=None)
def module_source_text(paths, module_name: str) -> str:
    return module_name_to_path(paths.project_root, module_name).read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def module_line_starts(paths, module_name: str) -> list[int]:
    return build_line_starts(module_source_text(paths, module_name))


@lru_cache(maxsize=None)
def sanitized_module_source_text(paths, module_name: str) -> str:
    return strip_lean_comments_and_strings(module_source_text(paths, module_name))


def clear_module_metadata_caches() -> None:
    load_ilean_metadata.cache_clear()
    module_decl_entries_from_ilean.cache_clear()
    module_source_text.cache_clear()
    module_line_starts.cache_clear()
    sanitized_module_source_text.cache_clear()
    module_header_insertion_offset.cache_clear()
    direct_imports_from_source.cache_clear()


@lru_cache(maxsize=None)
def module_decl_entries_from_ilean(paths, module_name: str) -> list[dict[str, object]]:
    metadata = load_ilean_metadata(paths, module_name)
    decls = metadata.get("decls", {})
    if not isinstance(decls, dict):
        fail("`.ilean` 里的 `decls` 字段格式异常。", details=[f"模块：`{module_name}`"])
    relative_file = relative_path_str(paths.project_root, module_name_to_path(paths.project_root, module_name))
    entries: list[dict[str, object]] = []
    for decl_name, raw in decls.items():
        if not isinstance(decl_name, str):
            continue
        if not isinstance(raw, list) or len(raw) < 4:
            continue
        entries.append(
            {
                "decl_name": user_visible_decl_name(module_name, decl_name),
                "implementation_name": decl_name,
                "module": module_name,
                "file": relative_file,
                "range": normalize_range(raw),
            }
        )
    return sorted(entries, key=lambda item: (range_key(item), str(item["decl_name"])))


def direct_host_imports_from_metadata(paths, metadata: dict[str, Any]) -> list[str]:
    imports: list[str] = []
    for item in metadata.get("directImports", []):
        if not isinstance(item, list) or not item:
            continue
        imported = str(item[0])
        if is_session_host_module(paths, imported) and imported not in imports:
            imports.append(imported)
    return imports


@lru_cache(maxsize=None)
def direct_imports_from_source(paths, module_name: str) -> tuple[str, ...]:
    path = module_name_to_path(paths.project_root, module_name)
    imports: list[str] = []
    in_block_comment = False
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n")
            stripped = line.strip()
            if in_block_comment:
                if "-/" in stripped:
                    in_block_comment = False
                continue
            if not stripped:
                continue
            if stripped.startswith("/-"):
                if "-/" not in stripped:
                    in_block_comment = True
                continue
            if stripped.startswith("--"):
                continue
            if stripped == "module":
                continue
            match = IMPORT_RE.match(line)
            if match is None:
                break
            for imported in match.group("mods").split():
                if imported not in imports:
                    imports.append(imported)
    return tuple(imports)


def direct_host_imports_from_source(paths, module_name: str) -> tuple[str, ...]:
    return tuple(
        imported
        for imported in direct_imports_from_source(paths, module_name)
        if is_session_host_module(paths, imported)
    )


def source_host_module_closure(
    paths,
    root_module: str,
    *,
    imports_cache: dict[str, tuple[str, ...]] | None = None,
) -> tuple[list[str], dict[str, tuple[str, ...]]]:
    cached_imports = imports_cache if imports_cache is not None else {}
    queue: deque[str] = deque([root_module])
    seen: set[str] = set()
    ordered: list[str] = []
    while queue:
        module_name = queue.popleft()
        if module_name in seen:
            continue
        seen.add(module_name)
        ordered.append(module_name)
        imports = cached_imports.get(module_name)
        if imports is None:
            imports = direct_host_imports_from_source(paths, module_name)
            cached_imports[module_name] = imports
        for imported in imports:
            queue.append(imported)
    return ordered, {module_name: cached_imports[module_name] for module_name in ordered}


def trace_source_hash(paths, module_name: str) -> str | None:
    trace_path = trace_path_for_module(paths, module_name)
    if not trace_path.exists():
        return None
    trace_data = read_json(trace_path)
    inputs = trace_data.get("inputs", [])
    if not isinstance(inputs, list):
        return None
    source_path = module_name_to_path(paths.project_root, module_name).resolve()
    relative_source_suffix = module_name_to_relative_path(module_name).as_posix()
    for item in inputs:
        if not isinstance(item, list) or len(item) < 2:
            continue
        caption = str(item[0])
        value = item[1]
        if not isinstance(value, str):
            continue
        caption_matches = False
        try:
            caption_matches = Path(caption).resolve() == source_path
        except OSError:
            caption_matches = False
        if not caption_matches:
            caption_matches = caption.replace("\\", "/").endswith(relative_source_suffix)
        if not caption_matches:
            continue
        normalized = value.strip().lower()
        if re.fullmatch(r"[0-9a-f]{16}", normalized):
            return normalized
    return None


def collect_artifact_issue_map(
    paths,
    modules: list[str],
    *,
    source_imports_by_module: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, ArtifactIssue]:
    imports_by_module = source_imports_by_module if source_imports_by_module is not None else {}
    issue_by_module: dict[str, ArtifactIssue] = {}
    modules_requiring_hash_check: list[str] = []
    trace_hashes: dict[str, str] = {}
    for current_module in modules:
        ilean_path = ilean_path_for_module(paths, current_module)
        olean_path = olean_path_for_module(paths, current_module)
        trace_path = trace_path_for_module(paths, current_module)
        missing: list[str] = []
        if not ilean_path.exists():
            missing.append(relative_path_str(paths.project_root, ilean_path))
        if not olean_path.exists():
            missing.append(relative_path_str(paths.project_root, olean_path))
        if not trace_path.exists():
            missing.append(relative_path_str(paths.project_root, trace_path))
        if missing:
            issue_by_module[current_module] = ArtifactIssue(
                module_name=current_module,
                detail="缺少构建产物：`" + "`, `".join(missing) + "`",
            )
            continue
        metadata = load_ilean_metadata(paths, current_module)
        metadata_imports = direct_host_imports_from_metadata(paths, metadata)
        source_imports = imports_by_module.get(current_module)
        if source_imports is None:
            source_imports = direct_host_imports_from_source(paths, current_module)
            imports_by_module[current_module] = source_imports
        if tuple(metadata_imports) != source_imports:
            issue_by_module[current_module] = ArtifactIssue(
                module_name=current_module,
                detail="当前源码 import 列表与 `.ilean` 记录不一致。",
            )
            continue
        recorded_source_hash = trace_source_hash(paths, current_module)
        if recorded_source_hash is None:
            issue_by_module[current_module] = ArtifactIssue(
                module_name=current_module,
                detail="`.trace` 中缺少当前源码文件的哈希记录。",
            )
            continue
        trace_hashes[current_module] = recorded_source_hash
        modules_requiring_hash_check.append(current_module)
    if not modules_requiring_hash_check:
        return issue_by_module
    source_paths = [module_name_to_path(paths.project_root, module_name).resolve() for module_name in modules_requiring_hash_check]
    current_hashes = compute_text_file_hashes(paths, source_paths)
    for current_module in modules_requiring_hash_check:
        source_path = module_name_to_path(paths.project_root, current_module).resolve()
        current_hash = current_hashes.get(source_path)
        if current_hash is None:
            fail(
                "内部错误：缺少当前源码文件的文本哈希。",
                details=[
                    f"模块：`{current_module}`",
                    f"源码：`{relative_path_str(paths.project_root, source_path)}`",
                ],
            )
        if current_hash != trace_hashes[current_module]:
            issue_by_module[current_module] = ArtifactIssue(
                module_name=current_module,
                detail=f"当前源码内容与 `.trace` 记录不一致：`{relative_path_str(paths.project_root, source_path)}`",
            )
    return issue_by_module


def collect_artifact_issue_state_for_roots(
    paths,
    root_modules: list[str],
) -> tuple[dict[str, list[str]], dict[str, ArtifactIssue]]:
    imports_cache: dict[str, tuple[str, ...]] = {}
    root_closures: dict[str, list[str]] = {}
    union_modules: list[str] = []
    seen_union: set[str] = set()
    for root_module in root_modules:
        closure, _ = source_host_module_closure(paths, root_module, imports_cache=imports_cache)
        root_closures[root_module] = closure
        for module_name in closure:
            if module_name in seen_union:
                continue
            seen_union.add(module_name)
            union_modules.append(module_name)
    issue_by_module = collect_artifact_issue_map(
        paths,
        union_modules,
        source_imports_by_module=imports_cache,
    )
    return root_closures, issue_by_module


def artifact_issues_for_closure(
    closure: list[str],
    issue_by_module: dict[str, ArtifactIssue],
) -> list[ArtifactIssue]:
    return [issue_by_module[module_name] for module_name in closure if module_name in issue_by_module]


def dedupe_artifact_issues(issues: list[ArtifactIssue]) -> list[ArtifactIssue]:
    seen: set[tuple[str, str]] = set()
    unique: list[ArtifactIssue] = []
    for issue in issues:
        key = (issue.module_name, issue.detail)
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)
    return unique


def format_artifact_issue_preview(issues: list[ArtifactIssue], *, limit: int = 10) -> str:
    preview = issues[:limit]
    lines = [f"- {issue.module_name}: {issue.detail}" for issue in preview]
    if len(issues) > len(preview):
        lines.append(f"- 其余模块：{len(issues) - len(preview)} 个")
    return "\n".join(lines)


def fail_for_artifact_issues(
    paths,
    *,
    requested_target: str,
    root_modules: list[str],
    issues: list[ArtifactIssue],
    auto_build_hint: bool,
) -> None:
    unique_issues = dedupe_artifact_issues(issues)
    hints = []
    if len(root_modules) == 1:
        hints.append(f"先运行 `lake build {root_modules[0]}`，或直接运行 `lake build`。")
    else:
        hints.append("先运行 `lake build`，确保候选目标模块及其依赖产物都是最新的。")
    if auto_build_hint:
        hints.append(
            f"如需由 `prepare` 自动补构建，可重试：`python3 scripts/temporary_axiom_session.py prepare --target {requested_target} --auto-build`。"
        )
    fail(
        "`prepare` 需要与当前源码一致的模块产物，但当前模块闭包尚未就绪。",
        details=[
            f"请求 target：`{requested_target}`",
            f"涉及根模块：{', '.join(root_modules)}",
            f"需要刷新的模块：{len(unique_issues)}",
            "示例：\n" + format_artifact_issue_preview(unique_issues),
        ],
        hints=hints,
    )


def ensure_module_artifacts(paths, module_name: str) -> None:
    build_module(paths, module_name)
    clear_module_metadata_caches()


def ensure_module_artifacts_ready(
    paths,
    *,
    requested_target: str,
    root_module: str,
    auto_build: bool,
) -> list[str]:
    root_closures, issue_by_module = collect_artifact_issue_state_for_roots(paths, [root_module])
    issues = dedupe_artifact_issues(artifact_issues_for_closure(root_closures[root_module], issue_by_module))
    if not issues:
        return root_closures[root_module]
    if not auto_build:
        fail_for_artifact_issues(
            paths,
            requested_target=requested_target,
            root_modules=[root_module],
            issues=issues,
            auto_build_hint=True,
        )
    print(f"Refreshing artifacts for `{root_module}` because `--auto-build` was set...", flush=True)
    print(format_artifact_issue_preview(issues), flush=True)
    ensure_module_artifacts(paths, root_module)
    refreshed_closure, _ = source_host_module_closure(paths, root_module)
    return refreshed_closure


def ensure_roots_artifacts_ready(
    paths,
    *,
    requested_target: str,
    root_modules: list[str],
    auto_build: bool,
) -> dict[str, list[str]]:
    if not root_modules:
        return {}
    root_closures, issue_by_module = collect_artifact_issue_state_for_roots(paths, root_modules)
    if not auto_build:
        blocked: list[ArtifactIssue] = []
        for root_module in root_modules:
            blocked.extend(artifact_issues_for_closure(root_closures[root_module], issue_by_module))
        if blocked:
            fail_for_artifact_issues(
                paths,
                requested_target=requested_target,
                root_modules=root_modules,
                issues=blocked,
                auto_build_hint=True,
            )
        return root_closures
    remaining_issue_by_module = dict(issue_by_module)
    for root_module in root_modules:
        issues = dedupe_artifact_issues(
            artifact_issues_for_closure(root_closures[root_module], remaining_issue_by_module)
        )
        if not issues:
            continue
        print(f"Refreshing artifacts for `{root_module}` because `--auto-build` was set...", flush=True)
        print(format_artifact_issue_preview(issues), flush=True)
        ensure_module_artifacts(paths, root_module)
        refreshed_closure, _ = source_host_module_closure(paths, root_module)
        root_closures[root_module] = refreshed_closure
        for module_name in refreshed_closure:
            remaining_issue_by_module.pop(module_name, None)
    return root_closures


def try_decl_range_from_ilean(paths, module_name: str, decl_name: str) -> dict[str, int] | None:
    metadata = load_ilean_metadata(paths, module_name)
    decls = metadata.get("decls", {})
    raw = decls.get(decl_name)
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) < 4:
        fail(
            "`.ilean` 里的 declaration range 格式异常。",
            details=[f"模块：`{module_name}`", f"声明：`{decl_name}`"],
            hints=[f"重新构建模块后再试：`lake build {module_name}`。"],
        )
    return normalize_range(raw)


def dedupe_decl_entries_by_visible_name(entries: list[dict[str, object]]) -> list[dict[str, object]]:
    deduped: list[dict[str, object]] = []
    seen: set[str] = set()
    for entry in entries:
        decl_name = str(entry["decl_name"])
        if decl_name in seen:
            continue
        seen.add(decl_name)
        deduped.append(entry)
    return deduped


@lru_cache(maxsize=None)
def module_header_insertion_offset(paths, module_name: str) -> int:
    text = module_source_text(paths, module_name)
    offset = 0
    in_block_comment = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if in_block_comment:
            offset += len(line)
            if "-/" in stripped:
                in_block_comment = False
            continue
        if not stripped:
            offset += len(line)
            continue
        if stripped.startswith("/-"):
            offset += len(line)
            if "-/" not in stripped:
                in_block_comment = True
            continue
        if stripped.startswith("--"):
            offset += len(line)
            continue
        if stripped in {"module", "prelude"}:
            offset += len(line)
            continue
        if IMPORT_RE.match(line):
            offset += len(line)
            continue
        return offset
    return len(text)


def module_entries_require_local_replay(module_entries: list[dict[str, object]]) -> bool:
    return False


def entry_identity_key(entry: dict[str, object]) -> tuple[str, tuple[int, int]]:
    return (str(entry["decl_name"]), range_key(entry))


def select_hashed_entries(
    hashed_entries: list[dict[str, object]],
    selected_entries: list[dict[str, object]],
) -> list[dict[str, object]]:
    wanted_keys = {entry_identity_key(entry) for entry in selected_entries}
    return [entry for entry in hashed_entries if entry_identity_key(entry) in wanted_keys]


def collect_decl_entries_via_import_probe(
    paths,
    *,
    module_name: str,
    module_entries: list[dict[str, object]],
    description_prefix: str,
) -> list[dict[str, object]]:
    result, payloads = run_lean_probe(
        paths,
        imports=[module_name],
        command_lines=[
            f"#print_temporary_axiom_decl_probe_text {lean_string_literal(entry_probe_decl_name(entry))}"
            for entry in module_entries
        ],
        description=f"{description_prefix} `{module_name}`",
        allow_failure=True,
    )
    if result.returncode != 0:
        output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
        if "object file '" in output and " does not exist" in output:
            return collect_decl_entries_via_replay(
                paths,
                module_name=module_name,
                module_entries=module_entries,
                description_prefix="缺少可导入产物，退回到模块内 replay 探测 theorem-side statement hash in",
            )
        fail(
            "Lean probe 执行失败。",
            details=[
                f"模块：`{module_name}`",
                "输出：\n" + (output or "<空>"),
            ],
        )
    payload_by_name = {str(payload["requested_name"]): payload for payload in payloads}
    missing = [
        entry_probe_decl_name(entry)
        for entry in module_entries
        if entry_probe_decl_name(entry) not in payload_by_name
    ]
    if missing:
        preview = missing[:10]
        details = [
            f"模块：`{module_name}`",
            f"缺失数量：{len(missing)}",
            "缺失声明：\n" + "\n".join(f"- {decl_name}" for decl_name in preview),
        ]
        output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
        if output:
            details.append("probe 输出：\n" + output)
        if len(missing) > len(preview):
            details.append(f"其余缺失条目：{len(missing) - len(preview)} 个")
        fail("Lean probe 没有返回全部候选声明。", details=details)
    return [
        normalize_probed_decl(
            payload_by_name[entry_probe_decl_name(entry)],
            module_name=str(entry["module"]),
            relative_file=str(entry["file"]),
            range_info=entry["range"],
        )
        for entry in module_entries
    ]


def build_collect_replay_source(
    paths,
    *,
    module_name: str,
    module_entries: list[dict[str, object]],
) -> str:
    return module_source_text(paths, module_name)


def build_collect_replay_payload(module_entries: list[dict[str, object]]) -> str:
    candidate_names: list[str] = []
    seen: set[str] = set()
    for entry in module_entries:
        decl_name = str(entry["decl_name"])
        if decl_name in seen:
            continue
        seen.add(decl_name)
        candidate_names.append(decl_name)
    return "\n".join(candidate_names)


def collect_decl_entries_via_replay(
    paths,
    *,
    module_name: str,
    module_entries: list[dict[str, object]],
    description_prefix: str,
) -> list[dict[str, object]]:
    replay_root = paths.session_root / "replay_candidates"
    replay_root.mkdir(parents=True, exist_ok=True)
    payload_path = replay_root / f"{module_name}.txt"
    payload_path.write_text(build_collect_replay_payload(module_entries), encoding="utf-8")
    try:
        result, payloads = run_lean_module_source(
            paths,
            module_name=module_name,
            source=build_collect_replay_source(
                paths,
                module_name=module_name,
                module_entries=module_entries,
            ),
            description=f"{description_prefix} `{module_name}`",
            allow_failure=True,
        )
    finally:
        payload_path.unlink(missing_ok=True)
    if result.returncode != 0:
        output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
        fail(
            "模块内 theorem replay 失败。",
            details=[
                f"模块：`{module_name}`",
                "输出：\n" + (output or "<空>"),
            ],
        )
    payload_by_name = {
        str(payload["decl_name"]): payload
        for payload in payloads
        if isinstance(payload, dict) and "decl_name" in payload and "statement_hash" in payload
    }
    missing = [
        str(entry["decl_name"])
        for entry in module_entries
        if str(entry["decl_name"]) not in payload_by_name
    ]
    if missing:
        preview = missing[:10]
        details = [
            f"模块：`{module_name}`",
            f"缺失数量：{len(missing)}",
            "缺失声明：\n" + "\n".join(f"- {decl_name}" for decl_name in preview),
        ]
        output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
        if output:
            details.append("replay 输出：\n" + output)
        if len(missing) > len(preview):
            details.append(f"其余缺失条目：{len(missing) - len(preview)} 个")
        fail("模块内 theorem replay 没有返回全部候选声明。", details=details)
    return [
        normalize_probed_decl(
            payload_by_name[str(entry["decl_name"])],
            module_name=str(entry["module"]),
            relative_file=str(entry["file"]),
            range_info=entry["range"],
        )
        for entry in module_entries
    ]


def scan_all_theorem_like_entries(
    paths,
    module_name: str,
    *,
    before_entry: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    return scan_theorem_like_entries(
        paths,
        module_name,
        before_entry=before_entry,
        include_proved=True,
        include_sorry=True,
    )


def probe_target_decl(
    paths,
    *,
    decl_name: str,
    module_name: str,
    range_info: dict[str, int],
) -> dict[str, object]:
    matching_entries = [
        entry
        for entry in module_decl_entries_from_ilean(paths, module_name)
        if str(entry["decl_name"]) == decl_name and dict(entry["range"]) == range_info
    ]
    if not matching_entries:
        fail(
            "无法从 `.ilean` 中定位目标声明。",
            details=[f"目标声明：`{decl_name}`", f"目标模块：`{module_name}`"],
        )
    target_entry = matching_entries[0]
    context_entries = [
        entry
        for entry in scan_all_theorem_like_entries(paths, module_name)
        if range_key(entry) <= range_key(target_entry)
    ]
    if module_entries_require_local_replay(context_entries):
        probed_entries = select_hashed_entries(
            collect_decl_entries_via_replay(
                paths,
                module_name=module_name,
                module_entries=context_entries,
                description_prefix="重放目标声明所在模块的 theorem-like 上下文 in",
            ),
            [target_entry],
        )
    else:
        probed_entries = probe_decl_entries(
            paths,
            candidates=[target_entry],
            description_prefix="探测目标声明的 theorem-side statement hash in",
        )
    if len(probed_entries) != 1:
        fail(
            "目标声明的 theorem-side hash 探测结果数量异常。",
            details=[f"目标声明：`{decl_name}`", f"目标模块：`{module_name}`", f"返回数量：`{len(probed_entries)}`"],
        )
    payload = probed_entries[0]
    resolved_module = str(payload["module"])
    if resolved_module != module_name:
        fail(
            "Lean probe 返回的模块与 `.ilean` 定位结果不一致。",
            details=[
                f"目标声明：`{decl_name}`",
                f"`.ilean` 定位模块：`{module_name}`",
                f"Lean probe 返回模块：`{resolved_module}`",
            ],
            hints=["如果这是 re-export 场景，请直接使用定义该声明的模块。"],
        )
    path = module_name_to_path(paths.project_root, module_name)
    return normalize_probed_decl(
        payload,
        module_name=module_name,
        relative_file=relative_path_str(paths.project_root, path),
        range_info=range_info,
    )


def resolve_decl_reference_in_module(
    paths,
    *,
    module_name: str,
    decl_name: str,
    decl_is_short_name: bool,
) -> tuple[str, dict[str, int]]:
    short_name = short_decl_name(decl_name)
    if not decl_is_short_name:
        range_info = try_decl_range_from_ilean(paths, module_name, decl_name)
        if range_info is not None:
            return decl_name, range_info
    raw_candidates = [
        entry
        for entry in module_decl_entries_from_ilean(paths, module_name)
        if short_decl_name(str(entry["decl_name"])) == short_name
    ]
    if not raw_candidates:
        fail(
            "指定模块中找不到目标声明。",
            details=[f"目标声明：`{decl_name}`", f"目标模块：`{module_name}`"],
            hints=[
                "这里的模块部分必须是声明的定义模块，而不只是 re-export 它的模块。",
                "如果该声明名在 Lean 里带额外 namespace，可以改用 `--target <module>:<fully-qualified-decl>`。",
            ],
        )
    visible_candidates = dedupe_decl_entries_by_visible_name(
        probe_decl_entries(
            paths,
            candidates=raw_candidates,
            description_prefix="解析目标声明的公开名 in",
        )
    )
    if decl_is_short_name:
        matches = [entry for entry in visible_candidates if short_decl_name(str(entry["decl_name"])) == decl_name]
    else:
        matches = [entry for entry in visible_candidates if str(entry["decl_name"]) == decl_name]
    if not matches:
        fail(
            "指定模块中找不到目标声明。",
            details=[f"目标声明：`{decl_name}`", f"目标模块：`{module_name}`"],
            hints=[
                "这里的模块部分必须是声明的定义模块，而不只是 re-export 它的模块。",
                "如果你只有完整声明名，也可以直接使用 `--target <fully-qualified-decl>`。",
            ],
        )
    if len(matches) > 1:
        candidates = "\n".join(f"- {entry['decl_name']}" for entry in matches[:10])
        details = [
            f"目标声明：`{decl_name}`",
            f"目标模块：`{module_name}`",
            "匹配到多个声明：\n" + candidates,
        ]
        if len(matches) > 10:
            details.append(f"其余候选：{len(matches) - 10} 个")
        fail(
            "指定模块中的目标声明不唯一。",
            details=details,
            hints=["请改用 `--target <module>:<fully-qualified-decl>` 明确指定目标声明。"],
        )
    match = matches[0]
    return str(match["decl_name"]), dict(match["range"])


def resolve_target_decl(
    paths,
    *,
    decl_name: str,
    module_name: str | None,
    decl_is_short_name: bool = False,
    candidate_modules: list[str] | None = None,
) -> dict[str, object]:
    if module_name is not None:
        if not is_host_project_module(paths.project_root, module_name):
            fail(
                "指定的目标模块不存在于当前项目中。",
                details=[f"目标声明：`{decl_name}`", f"目标模块：`{module_name}`"],
                hints=["确认 `--target` 里模块部分传入的是项目内的 Lean 模块名。"],
            )
        resolved_decl_name, range_info = resolve_decl_reference_in_module(
            paths,
            module_name=module_name,
            decl_name=decl_name,
            decl_is_short_name=decl_is_short_name,
        )
        return probe_target_decl(
            paths,
            decl_name=resolved_decl_name,
            module_name=module_name,
            range_info=range_info,
        )
    candidate_modules = (
        candidate_modules if candidate_modules is not None else candidate_target_modules_from_decl_name(paths, decl_name)
    )
    if not candidate_modules:
        fail(
            "无法从完整声明名推断候选模块。",
            details=[f"目标声明：`{decl_name}`"],
            hints=["如果该声明名与模块路径不对齐，请改用 `--target <module>:<decl>`。"],
        )
    for candidate_module in candidate_modules:
        raw_candidates = [
            entry
            for entry in module_decl_entries_from_ilean(paths, candidate_module)
            if short_decl_name(str(entry["decl_name"])) == short_decl_name(decl_name)
        ]
        if not raw_candidates:
            continue
        visible_candidates = dedupe_decl_entries_by_visible_name(
            probe_decl_entries(
                paths,
                candidates=raw_candidates,
                description_prefix="解析候选目标声明的公开名 in",
            )
        )
        matches = [entry for entry in visible_candidates if str(entry["decl_name"]) == decl_name]
        if len(matches) != 1:
            continue
        match = matches[0]
        return probe_target_decl(
            paths,
            decl_name=decl_name,
            module_name=candidate_module,
            range_info=dict(match["range"]),
        )
    fail(
        "Lean 无法解析目标声明。",
        details=[
            f"目标声明：`{decl_name}`",
            "尝试过的候选模块：\n" + "\n".join(f"- {candidate}" for candidate in candidate_modules),
        ],
        hints=[
            "确认 `--target` 是完整声明名。",
            "如果声明名与模块路径不完全对齐，请改用 `--target <module>:<decl>`。",
        ],
    )


def compute_module_closure(paths, root_module: str) -> list[str]:
    closure, _ = source_host_module_closure(paths, root_module)
    return closure


def dependency_first_module_order(paths, modules: list[str]) -> list[str]:
    modules_set = set(modules)
    ordered: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module_name: str) -> None:
        if module_name in visited or module_name not in modules_set:
            return
        if module_name in visiting:
            return
        visiting.add(module_name)
        for imported in direct_host_imports_from_source(paths, module_name):
            visit(imported)
        visiting.remove(module_name)
        visited.add(module_name)
        ordered.append(module_name)

    for module_name in modules:
        visit(module_name)
    return ordered


def group_entries_by_module(entries: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for entry in entries:
        grouped[str(entry["module"])].append(entry)
    return {module_name: grouped[module_name] for module_name in sorted(grouped)}


def merge_module_lists(*module_lists: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for module_list in module_lists:
        for module_name in module_list:
            if module_name in seen:
                continue
            seen.add(module_name)
            merged.append(module_name)
    return merged


def collect_tracked_theorems_by_build(
    paths,
    *,
    collect_modules: list[str],
) -> list[dict[str, object]]:
    if not collect_modules:
        return []
    collect_module_set = {str(module_name) for module_name in collect_modules}
    collected_by_key: dict[tuple[str, str], dict[str, object]] = {}
    for module_name in dependency_first_module_order(paths, collect_modules):
        result = run_command(
            paths,
            ["lake", "build", module_name],
            f"collect 模式构建 `{module_name}` 并收集 theorem-side hash",
            allow_failure=True,
        )
        if result.returncode != 0:
            output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
            fail(
                "collect 模式构建失败。",
                details=[
                    f"模块：`{module_name}`",
                    "输出：\n" + (output or "<空>"),
                ],
            )
        payloads = parse_json_payload_lines(result.stdout, result.stderr)
        for payload in payloads:
            if str(payload.get("kind", "")) != COLLECT_PAYLOAD_KIND:
                continue
            payload_module = str(payload.get("module", ""))
            if payload_module not in collect_module_set:
                continue
            normalized = normalize_collected_decl(paths, payload)
            collected_by_key[(str(normalized["module"]), str(normalized["decl_name"]))] = normalized
    return sorted(
        collected_by_key.values(),
        key=lambda item: (str(item["module"]), int(item["ordinal"]), str(item["decl_name"])),
    )


def collect_module_theorems_by_local_replay(
    paths,
    *,
    module_name: str,
) -> list[dict[str, object]]:
    shard_source = generated_shard_module_source(
        module_name=module_name,
        mode="collect",
        target_decl=None,
        target_hash=None,
        permitted_axioms=[],
    )
    shard_module_name = generated_shard_runtime_module_name(module_name)
    shard_path = paths.project_root / module_name_to_relative_path(generated_shard_runtime_module_name(module_name))
    shard_artifact_base = paths.lean_build_lib_root / module_name_to_relative_path(shard_module_name).with_suffix("")
    original_shard_source = shard_path.read_text(encoding="utf-8") if shard_path.exists() else None
    hidden_artifacts: list[tuple[Path, Path]] = []
    ensure_layout(paths)
    try:
        if original_shard_source != shard_source:
            shard_path.parent.mkdir(parents=True, exist_ok=True)
            shard_path.write_text(shard_source, encoding="utf-8")
        for artifact_path in sorted(shard_artifact_base.parent.glob(shard_artifact_base.name + ".*")):
            hidden_path = artifact_path.with_name(artifact_path.name + ".temporary_axiom_replay_backup")
            artifact_path.rename(hidden_path)
            hidden_artifacts.append((artifact_path, hidden_path))
        compile_importable_lean_module(
            paths,
            source_path=shard_path,
            module_name=shard_module_name,
            description=f"编译 collect shard `{shard_module_name}`",
        )
        result, _ = run_lean_module_file(
            paths,
            module_path=module_name_to_path(paths.project_root, module_name),
            description=f"重放 `{module_name}` 并收集 theorem-side hash",
            allow_failure=True,
        )
    finally:
        for artifact_path, hidden_path in reversed(hidden_artifacts):
            if hidden_path.exists():
                hidden_path.rename(artifact_path)
        if original_shard_source is None:
            shard_path.unlink(missing_ok=True)
        elif shard_path.read_text(encoding="utf-8") != original_shard_source:
            shard_path.write_text(original_shard_source, encoding="utf-8")
    if result.returncode != 0:
        output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
        fail(
            "tracked 模块的本地 collect replay 失败。",
            details=[
                f"模块：`{module_name}`",
                "输出：\n" + (output or "<空>"),
            ],
        )
    collected_by_key: dict[tuple[str, str], dict[str, object]] = {}
    for payload in parse_json_payload_lines(result.stdout, result.stderr):
        if str(payload.get("kind", "")) != COLLECT_PAYLOAD_KIND:
            continue
        payload_module = str(payload.get("module", ""))
        if payload_module != module_name:
            continue
        normalized = normalize_collected_decl(paths, payload)
        collected_by_key[(str(normalized["module"]), str(normalized["decl_name"]))] = normalized
    return sorted(
        collected_by_key.values(),
        key=lambda item: (str(item["module"]), int(item["ordinal"]), str(item["decl_name"])),
    )


def collect_tracked_theorems_by_local_replay(
    paths,
    *,
    replay_modules: list[str],
) -> list[dict[str, object]]:
    collected_entries: list[dict[str, object]] = []
    for module_name in replay_modules:
        collected_entries.extend(
            collect_module_theorems_by_local_replay(
                paths,
                module_name=module_name,
            )
        )
    return collected_entries


def resolve_target_from_collected_entries(
    *,
    decl_name: str,
    decl_is_short_name: bool,
    module_name: str,
    collected_entries: list[dict[str, object]],
) -> dict[str, object]:
    target_entries = [entry for entry in collected_entries if str(entry["module"]) == module_name]
    if decl_is_short_name:
        matches = [entry for entry in target_entries if short_decl_name(str(entry["decl_name"])) == decl_name]
    else:
        matches = [entry for entry in target_entries if str(entry["decl_name"]) == decl_name]
    if not matches:
        fail(
            "collect 结果中找不到目标声明。",
            details=[f"目标声明：`{decl_name}`", f"目标模块：`{module_name}`"],
            hints=[
                "如果这里传的是短名，请确认该短名在目标模块里唯一。",
                "如果需要精确指定，可改用 `--target <module>:<fully-qualified-decl>`。",
            ],
        )
    if len(matches) > 1:
        preview = "\n".join(f"- {entry['decl_name']}" for entry in matches[:10])
        details = [
            f"目标声明：`{decl_name}`",
            f"目标模块：`{module_name}`",
            "collect 结果中匹配到多个声明：\n" + preview,
        ]
        if len(matches) > 10:
            details.append(f"其余候选：{len(matches) - 10} 个")
        fail(
            "目标声明在 collect 结果中不唯一。",
            details=details,
            hints=["请改用 `--target <module>:<fully-qualified-decl>`。"],
        )
    return dict(matches[0])


def probe_decl_entries(
    paths,
    *,
    candidates: list[dict[str, object]],
    description_prefix: str,
) -> list[dict[str, object]]:
    if not candidates:
        return []
    grouped = group_entries_by_module(candidates)

    def run_probe_job(module_name: str, module_entries: list[dict[str, object]]) -> tuple[str, list[dict[str, object]]]:
        if module_entries_require_local_replay(module_entries):
            normalized = collect_decl_entries_via_replay(
                paths,
                module_name=module_name,
                module_entries=module_entries,
                description_prefix=description_prefix,
            )
        else:
            normalized = collect_decl_entries_via_import_probe(
                paths,
                module_name=module_name,
                module_entries=module_entries,
                description_prefix=description_prefix,
            )
        return module_name, normalized

    job_specs = list(grouped.items())
    max_workers = min(len(job_specs), max(1, min(os.cpu_count() or 1, MAX_PROBE_WORKERS)))
    if max_workers <= 1:
        job_results = [run_probe_job(module_name, module_entries) for module_name, module_entries in job_specs]
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(run_probe_job, module_name, module_entries) for module_name, module_entries in job_specs]
            job_results = [future.result() for future in futures]
    normalized_by_module = {module_name: normalized for module_name, normalized in job_results}
    normalized: list[dict[str, object]] = []
    for module_name, _ in job_specs:
        normalized.extend(normalized_by_module[module_name])
    return normalized


def scan_theorem_like_entries(
    paths,
    module_name: str,
    *,
    before_entry: dict[str, object] | None = None,
    include_proved: bool,
    include_sorry: bool,
) -> list[dict[str, object]]:
    sanitized_text = sanitized_module_source_text(paths, module_name)
    if not module_may_contain_theorem_like_sanitized(sanitized_text):
        return []
    text = module_source_text(paths, module_name)
    line_starts = module_line_starts(paths, module_name)
    if before_entry is None:
        scan_limit = None
    else:
        scan_limit = offset_for_line_column(
            text,
            line_starts,
            int(before_entry["range"]["line"]),
            int(before_entry["range"]["column"]),
        )
    candidates: list[dict[str, object]] = []
    for entry in module_decl_entries_from_ilean(paths, module_name):
        if before_entry is not None:
            if str(entry["decl_name"]) == str(before_entry["decl_name"]):
                continue
            if range_key(entry) >= range_key(before_entry):
                break
        snippet = slice_command_text_for_decl(sanitized_text, line_starts, entry["range"])
        if scan_limit is not None:
            start = offset_for_line_column(text, line_starts, int(entry["range"]["line"]), int(entry["range"]["column"]))
            if start >= scan_limit:
                break
        if include_sorry and decl_is_explicit_sorry_theorem_like_sanitized(snippet):
            candidates.append(entry)
            continue
        if include_proved and decl_is_proved_theorem_like_sanitized(snippet):
            candidates.append(entry)
    return candidates


def discover_tracked_modules(paths) -> list[str]:
    tracked: list[str] = []
    excluded_roots = {
        ".git",
        ".lake",
        ".temporary_axiom_session",
        ".temporary_axiom_registry",
        "docs",
    }
    for path in sorted(paths.project_root.rglob("*.lean")):
        try:
            relative = path.relative_to(paths.project_root)
        except ValueError:
            continue
        if not relative.parts:
            continue
        if relative.parts[0] in excluded_roots:
            continue
        if path.is_relative_to(paths.generated_shards_root):
            continue
        module_name = path_to_module_name(paths.project_root, path)
        if TOOL_ROOT_MODULE in direct_imports_from_source(paths, module_name):
            tracked.append(module_name)
    return tracked


def collect_registry_source_hashes(paths, modules: list[str]) -> dict[str, str]:
    source_paths = [module_name_to_path(paths.project_root, module_name).resolve() for module_name in modules]
    raw_hashes = compute_text_file_hashes(paths, source_paths)
    return {
        module_name: raw_hashes[module_name_to_path(paths.project_root, module_name).resolve()]
        for module_name in modules
    }


def empty_registry_db() -> dict[str, Any]:
    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "tracked_modules": [],
        "dirty_modules": [],
        "module_digests": {},
        "proved_theorems": [],
    }


def load_registry_db(paths) -> dict[str, Any]:
    if not paths.registry_db_file.exists():
        return empty_registry_db()
    payload = read_json(paths.registry_db_file)
    if not isinstance(payload, dict):
        fail("proved theorem registry 数据格式无效。", details=[f"文件：`{paths.registry_db_file}`"])
    if payload.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        return empty_registry_db()
    payload.setdefault("tracked_modules", [])
    payload.setdefault("dirty_modules", [])
    payload.setdefault("module_digests", {})
    payload.setdefault("proved_theorems", [])
    return payload


def write_registry_db(paths, payload: dict[str, Any]) -> None:
    ensure_layout(paths)
    write_json(paths.registry_db_file, payload)


def registry_entries_by_module(registry_db: dict[str, Any]) -> dict[str, list[dict[str, object]]]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for entry in registry_db.get("proved_theorems", []):
        if not isinstance(entry, dict):
            continue
        grouped[str(entry.get("module", ""))].append(entry)
    return {module_name: grouped[module_name] for module_name in sorted(grouped)}


def registry_permitted_axioms(registry_db: dict[str, Any]) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for entry in registry_db.get("proved_theorems", []):
        if not isinstance(entry, dict):
            continue
        try:
            entries.append(
                {
                    "decl_name": str(entry["decl_name"]),
                    "module": str(entry["module"]),
                    "file": str(entry.get("file", "")),
                    "statement_hash": str(entry["statement_hash"]),
                    "origin": "persistent_proved",
                }
            )
        except KeyError:
            continue
    return sorted(entries, key=lambda item: (str(item["module"]), str(item["decl_name"])))


def refresh_registry_for_modules_from_collect(
    registry_db: dict[str, Any],
    *,
    modules: list[str],
    digests_by_module: dict[str, str],
    collected_entries: list[dict[str, object]],
    tracked_modules: list[str],
    clear_dirty_modules: bool,
) -> dict[str, Any]:
    modules_set = {str(module_name) for module_name in modules}
    tracked_modules_set = {str(module_name) for module_name in tracked_modules}
    collected_by_module = group_entries_by_module(
        [
            entry
            for entry in collected_entries
            if str(entry["module"]) in modules_set and not bool(entry.get("explicit_sorry", False))
        ]
    )
    refreshed_entries = [
        entry
        for entry in registry_db.get("proved_theorems", [])
        if str(entry.get("module")) not in modules_set and str(entry.get("module")) in tracked_modules_set
    ]
    for module_name in sorted(modules_set):
        for entry in collected_by_module.get(module_name, []):
            refreshed_entries.append(
                {
                    "decl_name": str(entry["decl_name"]),
                    "module": str(entry["module"]),
                    "file": str(entry["file"]),
                    "statement_hash": str(entry["statement_hash"]),
                }
            )
    module_digests = dict(registry_db.get("module_digests", {}))
    for module_name in modules:
        digest = digests_by_module.get(module_name)
        if digest is not None:
            module_digests[module_name] = digest
    existing_dirty = {str(module_name) for module_name in registry_db.get("dirty_modules", [])}
    if clear_dirty_modules:
        dirty_modules = existing_dirty - modules_set
    else:
        dirty_modules = existing_dirty | modules_set
    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "tracked_modules": sorted(tracked_modules),
        "dirty_modules": sorted(dirty_modules),
        "module_digests": module_digests,
        "proved_theorems": sorted(
            refreshed_entries,
            key=lambda item: (str(item["module"]), str(item["decl_name"])),
        ),
    }


def collect_session_temporary_axioms_from_tracked(
    *,
    target_info: dict[str, object],
    tracked_modules: set[str],
    collected_entries: list[dict[str, object]],
) -> list[dict[str, object]]:
    target_module = str(target_info["module"])
    target_decl_name = str(target_info["decl_name"])
    target_ordinal = (
        int(target_info["ordinal"])
        if "ordinal" in target_info and target_info["ordinal"] is not None
        else None
    )
    temporary_entries: list[dict[str, object]] = []
    for entry in collected_entries:
        module_name = str(entry["module"])
        if module_name not in tracked_modules:
            continue
        if not bool(entry.get("explicit_sorry", False)):
            continue
        if module_name == target_module:
            if str(entry["decl_name"]) == target_decl_name:
                continue
            if target_ordinal is not None and int(entry["ordinal"]) >= target_ordinal:
                continue
        temporary_entries.append(
            {
                "decl_name": str(entry["decl_name"]),
                "module": module_name,
                "file": str(entry["file"]),
                "statement_hash": str(entry["statement_hash"]),
                "origin": "session_temporary",
            }
        )
    return sorted(
        temporary_entries,
        key=lambda item: (str(item["module"]), str(item["decl_name"])),
    )


def collect_session_temporary_axioms(
    paths,
    *,
    target_info: dict[str, object],
    tracked_modules: set[str],
    module_closure: list[str] | None = None,
) -> tuple[list[str], list[dict[str, object]]]:
    target_module = str(target_info["module"])
    closure = list(module_closure) if module_closure is not None else compute_module_closure(paths, target_module)
    temporary_entries: list[dict[str, object]] = []
    for module_name in closure:
        if module_name in tracked_modules:
            continue
        temporary_candidates = scan_theorem_like_entries(
            paths,
            module_name,
            before_entry=target_info if module_name == target_module else None,
            include_proved=False,
            include_sorry=True,
        )
        if not temporary_candidates:
            continue
        context_entries = scan_all_theorem_like_entries(
            paths,
            module_name,
            before_entry=target_info if module_name == target_module else None,
        )
        if module_entries_require_local_replay(context_entries):
            temporary_entries.extend(
                select_hashed_entries(
                    collect_decl_entries_via_replay(
                        paths,
                        module_name=module_name,
                        module_entries=context_entries,
                        description_prefix="重放模块并探测 session temporary theorem 的 theorem-side statement hash in",
                    ),
                    temporary_candidates,
                )
            )
        else:
            temporary_entries.extend(
                collect_decl_entries_via_import_probe(
                    paths,
                    module_name=module_name,
                    module_entries=temporary_candidates,
                    description_prefix="探测 session temporary theorem 的 theorem-side statement hash in",
                )
            )
    for entry in temporary_entries:
        entry["origin"] = "session_temporary"
    return closure, sorted(
        temporary_entries,
        key=lambda item: (str(item["module"]), int(item["range"]["line"]), int(item["range"]["column"]), str(item["decl_name"])),
    )


def merge_permitted_axioms(
    *,
    target_decl_name: str,
    persistent_axioms: list[dict[str, object]],
    session_temporary_axioms: list[dict[str, object]],
) -> list[dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    for entry in persistent_axioms:
        decl_name = str(entry["decl_name"])
        if decl_name == target_decl_name:
            continue
        merged[decl_name] = dict(entry)
    for entry in session_temporary_axioms:
        decl_name = str(entry["decl_name"])
        if decl_name == target_decl_name:
            continue
        merged[decl_name] = dict(entry)
    return sorted(
        merged.values(),
        key=lambda item: (str(item["module"]), str(item["decl_name"])),
    )


def merge_session_temporary_axioms(*temporary_groups: list[dict[str, object]]) -> list[dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    for group in temporary_groups:
        for entry in group:
            merged[str(entry["decl_name"])] = dict(entry)
    return sorted(
        merged.values(),
        key=lambda item: (str(item["module"]), str(item["decl_name"])),
    )


def compute_import_insertion_index(lines: list[str]) -> int:
    in_block_comment = False
    last_import_idx: int | None = None
    module_idx: int | None = None
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if in_block_comment:
            if "-/" in stripped:
                in_block_comment = False
            continue
        if not stripped:
            continue
        if stripped.startswith("/-"):
            if "-/" not in stripped:
                in_block_comment = True
            continue
        if stripped.startswith("--"):
            continue
        if stripped == "module":
            module_idx = idx
            continue
        if IMPORT_RE.match(line):
            last_import_idx = idx
            continue
        break
    if last_import_idx is not None:
        return last_import_idx + 1
    if module_idx is not None:
        return module_idx + 1
    return 0


def has_import_module(lines: list[str], module_name: str) -> bool:
    for line in lines:
        match = IMPORT_RE.match(line)
        if match is None:
            continue
        if module_name in match.group("mods").split():
            return True
    return False


def write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ensure_stable_shard_imports(paths, tracked_modules: list[str]) -> list[str]:
    changed_modules: list[str] = []
    for module_name in tracked_modules:
        path = module_name_to_path(paths.project_root, module_name)
        lines = path.read_text(encoding="utf-8").splitlines()
        import_module = generated_shard_runtime_module_name(module_name)
        if has_import_module(lines, import_module):
            continue
        insert_idx = compute_import_insertion_index(lines)
        lines.insert(insert_idx, f"import {import_module}")
        write_lines(path, lines)
        changed_modules.append(module_name)
    if changed_modules:
        clear_module_metadata_caches()
    return changed_modules


def load_session(paths) -> dict[str, Any]:
    if not paths.session_file.exists():
        fail(
            "当前没有可清理的活动 session。",
            details=[f"期望的 session 文件：`{relative_path_str(paths.project_root, paths.session_file)}`"],
            hints=["先运行 `prepare`，或确认上一次 session 是否已经被 cleanup。"],
        )
    return read_json(paths.session_file)


def write_session(paths, session_payload: dict[str, Any]) -> None:
    ensure_layout(paths)
    write_json(paths.session_file, session_payload)


def remove_session_artifacts(paths) -> None:
    paths.session_file.unlink(missing_ok=True)
    paths.report_file.unlink(missing_ok=True)
    try:
        paths.session_root.rmdir()
    except OSError:
        pass


def extract_session_target(session_payload: dict[str, Any]) -> dict[str, str]:
    try:
        target = session_payload["freeze"]["target"]
        decl_name = str(target["decl_name"])
        module_name = str(target["module"])
        statement_hash = str(target["statement_hash"])
    except (KeyError, TypeError):
        fail("活动 session 文件缺少 `freeze.target` 的必要字段。")
    return {"decl_name": decl_name, "module": module_name, "statement_hash": statement_hash}


def extract_session_tracked_modules(session_payload: dict[str, Any]) -> list[str]:
    try:
        raw_modules = session_payload["freeze"]["tracked_modules"]
    except (KeyError, TypeError):
        fail("活动 session 文件缺少 `freeze.tracked_modules` 字段。")
    if not isinstance(raw_modules, list):
        fail("活动 session 文件中的 `freeze.tracked_modules` 不是数组。")
    return [str(module_name) for module_name in raw_modules]


def extract_session_permitted_axioms(session_payload: dict[str, Any]) -> list[dict[str, str]]:
    try:
        raw_entries = session_payload["freeze"]["permitted_axioms"]
    except (KeyError, TypeError):
        fail("活动 session 文件缺少 `freeze.permitted_axioms` 字段。")
    if not isinstance(raw_entries, list):
        fail("活动 session 文件中的 `freeze.permitted_axioms` 不是数组。")
    entries: list[dict[str, str]] = []
    for item in raw_entries:
        if not isinstance(item, dict):
            fail("活动 session 文件中的 permitted axioms 条目格式无效。")
        try:
            entries.append(
                {
                    "decl_name": str(item["decl_name"]),
                    "module": str(item["module"]),
                    "statement_hash": str(item["statement_hash"]),
                    "origin": str(item.get("origin", "")),
                }
            )
        except KeyError:
            fail("活动 session 文件中的 permitted axioms 条目缺少必要字段。")
    return entries


def group_permitted_axioms_by_module(
    permitted_axioms: list[dict[str, object]],
) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for entry in permitted_axioms:
        grouped[str(entry["module"])].append(
            {
                "decl_name": str(entry["decl_name"]),
                "origin": str(entry["origin"]),
                "statement_hash": str(entry["statement_hash"]),
                "file": str(entry.get("file", "")),
            }
        )
    return {
        module_name: sorted(entries, key=lambda item: item["decl_name"])
        for module_name, entries in sorted(grouped.items())
    }


def session_report_text(
    *,
    session_payload: dict[str, Any],
    grouped_permitted_axioms: dict[str, list[dict[str, str]]],
) -> str:
    target = extract_session_target(session_payload)
    tracked_modules = extract_session_tracked_modules(session_payload)
    module_closure = session_payload["freeze"]["module_closure"]
    session_temporaries = session_payload["freeze"]["session_temporary_axioms"]
    lines = [
        "TemporaryAxiomTool prepared session report",
        "",
        "Session summary",
        f"- target: {target['decl_name']}",
        f"- target module: {target['module']}",
        f"- target statement hash: {target['statement_hash']}",
        f"- tracked modules: {len(tracked_modules)}",
        f"- target closure size: {len(module_closure)}",
        f"- session temporary axioms: {len(session_temporaries)}",
        f"- total permitted axioms: {sum(len(entries) for entries in grouped_permitted_axioms.values())}",
        "",
        "Tracked modules",
    ]
    lines.extend(f"- {module_name}" for module_name in tracked_modules)
    lines.append("")
    lines.append("Target closure")
    lines.extend(f"- {module_name}" for module_name in module_closure)
    lines.append("")
    lines.append("Permitted axioms by module")
    if not grouped_permitted_axioms:
        lines.append("- <none>")
    else:
        for module_name, entries in grouped_permitted_axioms.items():
            lines.append(f"- {module_name} ({len(entries)})")
            for entry in entries:
                lines.append(f"  - {entry['decl_name']} [{entry['origin']}]")
    lines.append("")
    lines.append("Artifacts")
    lines.append("- .temporary_axiom_session/session.json: freeze data for external tooling")
    lines.append("- temporary_axiom_tool_session_report.txt: human-readable session summary")
    lines.append("- .temporary_axiom_registry/proved_theorems.json: persistent proved theorem database")
    lines.append("- TemporaryAxiomTool/TheoremRegistry/Shards/**/*.lean: generated per-module registry shards")
    return "\n".join(lines) + "\n"


def write_prepare_reports(
    paths,
    *,
    session_payload: dict[str, Any],
    permitted_axioms: list[dict[str, object]],
) -> dict[str, list[dict[str, str]]]:
    ensure_layout(paths)
    grouped = group_permitted_axioms_by_module(permitted_axioms)
    paths.report_file.write_text(
        session_report_text(
            session_payload=session_payload,
            grouped_permitted_axioms=grouped,
        ),
        encoding="utf-8",
    )
    return grouped


def print_prepare_summary(
    paths,
    *,
    target_info: dict[str, object],
    tracked_modules: list[str],
    module_closure: list[str],
    grouped_permitted_axioms: dict[str, list[dict[str, str]]],
    session_temporary_count: int,
) -> None:
    permitted_count = sum(len(entries) for entries in grouped_permitted_axioms.values())
    print(f"Prepared session for `{target_info['decl_name']}`.")
    print(f"- target module: {target_info['module']}")
    print(f"- tracked modules: {len(tracked_modules)}")
    print(f"- target closure modules: {len(module_closure)}")
    print(f"- session temporary axioms: {session_temporary_count}")
    print(f"- total permitted axioms: {permitted_count}")
    if len(tracked_modules) <= INLINE_MODULE_LIST_LIMIT:
        print("- tracked module list:")
        for module_name in tracked_modules:
            print(f"  - {module_name}")
    if permitted_count <= INLINE_PERMITTED_AXIOM_LIMIT:
        print("- permitted axioms by module:")
        if not grouped_permitted_axioms:
            print("  - <none>")
        else:
            for module_name, entries in grouped_permitted_axioms.items():
                print(f"  - {module_name} ({len(entries)})")
                for entry in entries:
                    print(f"    - {entry['decl_name']} [{entry['origin']}]")
    else:
        print("- permitted axiom list is long; see the saved report for the full grouped list.")
    print("- verification: completed via a final target-module build")
    print(f"- session data: {relative_path_str(paths.project_root, paths.session_file)}")
    print(f"- report: {relative_path_str(paths.project_root, paths.report_file)}")
    print(f"- proved registry DB: {relative_path_str(paths.project_root, paths.registry_db_file)}")


def assert_no_active_session(paths) -> None:
    if not paths.session_file.exists():
        return
    session_payload = read_json(paths.session_file)
    active_target = extract_session_target(session_payload)
    fail(
        "已有活动 session。",
        details=[
            f"当前活动 target：`{active_target['decl_name']}`",
            f"statement hash：`{active_target['statement_hash']}`",
        ],
        hints=[
            "继续当前 proving attempt，或先执行 `cleanup` 再重新 `prepare`。",
        ],
    )


def prepare_session(args: argparse.Namespace, paths) -> None:
    acquire_prepare_lock(paths)
    updated_registry_db: dict[str, Any] | None = None
    current_tracked_modules: list[str] = []
    try:
        assert_no_active_session(paths)
        requested_target = parse_target_spec(args.target)
        current_tracked_modules = discover_tracked_modules(paths)
        if requested_target.module_name is not None and requested_target.module_name not in current_tracked_modules:
            fail(
                "目标模块尚未纳入 theorem registry 跟踪。",
                details=[f"目标模块：`{requested_target.module_name}`"],
                hints=[
                    f"在 `{requested_target.module_name}` 的源码 import 头里加入 `import {TOOL_ROOT_MODULE}`。",
                    "只有直接导入 TemporaryAxiomTool 的项目模块，才会在 session 中启用 theorem registry。",
                ],
            )
        inserted_shard_import_modules = ensure_stable_shard_imports(paths, current_tracked_modules)
        registry_db = load_registry_db(paths)
        current_source_hashes = collect_registry_source_hashes(paths, current_tracked_modules)
        dirty_modules = {str(module_name) for module_name in registry_db.get("dirty_modules", [])}
        changed_tracked_modules = [
            module_name
            for module_name in current_tracked_modules
            if (
                registry_db.get("module_digests", {}).get(module_name) != current_source_hashes[module_name]
                or module_name in dirty_modules
            )
        ]
        if inserted_shard_import_modules:
            changed_tracked_modules = merge_module_lists(changed_tracked_modules, inserted_shard_import_modules)

        target_info_from_probe: dict[str, object] | None = None
        if requested_target.module_name is None:
            candidate_modules = candidate_target_modules_from_decl_name(paths, requested_target.decl_name)
            if not candidate_modules:
                fail(
                    "无法从完整声明名推断候选模块。",
                    details=[f"目标声明：`{requested_target.decl_name}`"],
                    hints=["如果该声明名与模块路径不对齐，请改用 `--target <module>:<decl>`。"],
                )
            tracked_candidate_modules = [
                module_name for module_name in candidate_modules if module_name in current_tracked_modules
            ]
            if not tracked_candidate_modules:
                fail(
                    "目标候选模块尚未纳入 theorem registry 跟踪。",
                    details=["候选模块：\n" + "\n".join(f"- {module_name}" for module_name in candidate_modules)],
                    hints=["请在目标所在模块源码里直接 `import TemporaryAxiomTool` 后再重试。"],
                )
            print("Checking build artifacts for candidate target modules...", flush=True)
            ensure_roots_artifacts_ready(
                paths,
                requested_target=args.target,
                root_modules=tracked_candidate_modules,
                auto_build=args.auto_build,
            )
            ensure_probe_tool_ready(paths)
            target_info_from_probe = resolve_target_decl(
                paths,
                decl_name=requested_target.decl_name,
                module_name=None,
                decl_is_short_name=False,
                candidate_modules=tracked_candidate_modules,
            )
            target_module = str(target_info_from_probe["module"])
        else:
            target_module = requested_target.module_name

        if target_module not in current_tracked_modules:
            fail(
                "目标模块尚未纳入 theorem registry 跟踪。",
                details=[f"目标模块：`{target_module}`"],
                hints=[f"在 `{target_module}` 的源码 import 头里加入 `import {TOOL_ROOT_MODULE}`。"],
            )

        target_closure = compute_module_closure(paths, target_module)
        relevant_tracked_modules = [
            module_name
            for module_name in target_closure
            if module_name in current_tracked_modules
        ]
        changed_relevant_tracked_modules = [
            module_name for module_name in relevant_tracked_modules if module_name in changed_tracked_modules
        ]
        unchanged_relevant_tracked_modules = [
            module_name for module_name in relevant_tracked_modules if module_name not in changed_tracked_modules
        ]
        use_fast_prepare_path = not changed_relevant_tracked_modules
        collect_modules = dependency_first_module_order(
            paths,
            (
                changed_tracked_modules
                if use_fast_prepare_path
                else merge_module_lists(changed_tracked_modules, relevant_tracked_modules)
            ),
        )
        if collect_modules:
            print("Collecting theorem-side hashes for changed tracked modules...", flush=True)
            write_collect_shards(
                paths,
                tracked_modules=current_tracked_modules,
                collect_modules=collect_modules,
            )
            collected_entries = collect_tracked_theorems_by_build(
                paths,
                collect_modules=collect_modules,
            )
        else:
            collected_entries = []
        if use_fast_prepare_path:
            replay_modules = [target_module]
            replay_modules.extend(
                module_name
                for module_name in unchanged_relevant_tracked_modules
                if module_name != target_module
                and scan_theorem_like_entries(
                    paths,
                    module_name,
                    before_entry=None,
                    include_proved=False,
                    include_sorry=True,
                )
            )
            print("Locally replaying target / explicit-sorry tracked modules for theorem-side hashes...", flush=True)
            replayed_entries = collect_tracked_theorems_by_local_replay(
                paths,
                replay_modules=replay_modules,
            )
        else:
            replay_modules = []
            replayed_entries = []

        if use_fast_prepare_path:
            target_info = resolve_target_from_collected_entries(
                decl_name=(
                    str(target_info_from_probe["decl_name"])
                    if requested_target.module_name is None and target_info_from_probe is not None
                    else requested_target.decl_name
                ),
                decl_is_short_name=(
                    False if requested_target.module_name is None else requested_target.decl_is_short_name
                ),
                module_name=target_module,
                collected_entries=replayed_entries,
            )
        else:
            target_info = resolve_target_from_collected_entries(
                decl_name=(
                    str(target_info_from_probe["decl_name"])
                    if requested_target.module_name is None and target_info_from_probe is not None
                    else requested_target.decl_name
                ),
                decl_is_short_name=(
                    False if requested_target.module_name is None else requested_target.decl_is_short_name
                ),
                module_name=target_module,
                collected_entries=collected_entries,
            )

        if changed_tracked_modules:
            print("Refreshing proved theorem registry for changed tracked modules...", flush=True)
        updated_registry_db = refresh_registry_for_modules_from_collect(
            registry_db,
            modules=changed_tracked_modules,
            digests_by_module=current_source_hashes,
            collected_entries=collected_entries,
            tracked_modules=current_tracked_modules,
            clear_dirty_modules=True,
        )
        write_registry_db(paths, updated_registry_db)

        if use_fast_prepare_path:
            tracked_session_temporary_axioms = merge_session_temporary_axioms(
                collect_session_temporary_axioms_from_tracked(
                    target_info=target_info,
                    tracked_modules=set(changed_relevant_tracked_modules),
                    collected_entries=collected_entries,
                ),
                collect_session_temporary_axioms_from_tracked(
                    target_info=target_info,
                    tracked_modules=set(replay_modules),
                    collected_entries=replayed_entries,
                ),
            )
        else:
            tracked_session_temporary_axioms = collect_session_temporary_axioms_from_tracked(
                target_info=target_info,
                tracked_modules=set(relevant_tracked_modules),
                collected_entries=collected_entries,
            )
        ensure_probe_tool_ready(paths)
        print("Scanning target closure for session-local explicit `sorry` theorems...", flush=True)
        module_closure, untracked_session_temporary_axioms = collect_session_temporary_axioms(
            paths,
            target_info=target_info,
            tracked_modules=set(current_tracked_modules),
            module_closure=target_closure,
        )
        session_temporary_axioms = merge_session_temporary_axioms(
            tracked_session_temporary_axioms,
            untracked_session_temporary_axioms,
        )
        persistent_axioms = registry_permitted_axioms(updated_registry_db)
        permitted_axioms = merge_permitted_axioms(
            target_decl_name=str(target_info["decl_name"]),
            persistent_axioms=persistent_axioms,
            session_temporary_axioms=session_temporary_axioms,
        )
        write_active_shards(
            paths,
            tracked_modules=current_tracked_modules,
            target_decl=str(target_info["decl_name"]),
            target_hash=str(target_info["statement_hash"]),
            permitted_axioms=permitted_axioms,
        )
        print("Verifying prepared workspace by building the target module...", flush=True)
        target_build_result = run_command(
            paths,
            ["lake", "build", target_module],
            f"验证 prepared workspace 中 `{target_module}` 可以构建",
            allow_failure=True,
        )
        if target_build_result.returncode != 0:
            output = "\n".join(
                part for part in [target_build_result.stdout.strip(), target_build_result.stderr.strip()] if part
            )
            fail(
                "prepare 完成后，target 模块仍然无法构建。",
                details=[
                    f"目标模块：`{target_module}`",
                    "输出：\n" + (output or "<空>"),
                ],
                hints=["先检查当前报错是否来自 target 模块自身，或是否存在与 theorem-registry 无关的编译错误。"],
            )
        base_commit = args.base_commit if args.base_commit is not None else try_git_head(paths)
        session_payload = {
            "schema_version": SESSION_SCHEMA_VERSION,
            "base_commit": base_commit,
            "freeze": {
                "target": {
                    "decl_name": str(target_info["decl_name"]),
                    "module": target_module,
                    "statement_hash": str(target_info["statement_hash"]),
                },
                "tracked_modules": current_tracked_modules,
                "module_closure": module_closure,
                "session_temporary_axioms": [
                    {
                        "decl_name": str(entry["decl_name"]),
                        "module": str(entry["module"]),
                        "statement_hash": str(entry["statement_hash"]),
                        "origin": str(entry["origin"]),
                    }
                    for entry in session_temporary_axioms
                ],
                "permitted_axioms": [
                    {
                        "decl_name": str(entry["decl_name"]),
                        "module": str(entry["module"]),
                        "statement_hash": str(entry["statement_hash"]),
                        "origin": str(entry["origin"]),
                    }
                    for entry in permitted_axioms
                ],
            },
        }
        write_session(paths, session_payload)
        grouped_permitted_axioms = write_prepare_reports(
            paths,
            session_payload=session_payload,
            permitted_axioms=permitted_axioms,
        )
        print_prepare_summary(
            paths,
            target_info=target_info,
            tracked_modules=current_tracked_modules,
            module_closure=module_closure,
            grouped_permitted_axioms=grouped_permitted_axioms,
            session_temporary_count=len(session_temporary_axioms),
        )
    except BaseException:
        if current_tracked_modules:
            write_inactive_shards(
                paths,
                tracked_modules=current_tracked_modules,
            )
        remove_session_artifacts(paths)
        raise
    finally:
        release_prepare_lock(paths)


def cleanup_session(paths) -> None:
    session_payload = load_session(paths)
    current_tracked_modules = discover_tracked_modules(paths)
    inserted_shard_import_modules = ensure_stable_shard_imports(paths, current_tracked_modules)
    registry_db = load_registry_db(paths)
    current_hashes = collect_registry_source_hashes(paths, current_tracked_modules)
    dirty_modules = {str(module_name) for module_name in registry_db.get("dirty_modules", [])}
    changed_tracked_modules = [
        module_name
        for module_name in current_tracked_modules
        if (
                registry_db.get("module_digests", {}).get(module_name) != current_hashes[module_name]
                or module_name in dirty_modules
            )
        ]
    if inserted_shard_import_modules:
        changed_tracked_modules = merge_module_lists(changed_tracked_modules, inserted_shard_import_modules)
    if changed_tracked_modules:
        print("Refreshing proved theorem registry for changed tracked modules...", flush=True)
        write_collect_shards(
            paths,
            tracked_modules=current_tracked_modules,
            collect_modules=changed_tracked_modules,
        )
        collected_entries = collect_tracked_theorems_by_build(
            paths,
            collect_modules=changed_tracked_modules,
        )
    else:
        collected_entries = []
    updated_registry_db = refresh_registry_for_modules_from_collect(
        registry_db,
        modules=changed_tracked_modules,
        digests_by_module=current_hashes,
        collected_entries=collected_entries,
        tracked_modules=current_tracked_modules,
        clear_dirty_modules=True,
    )
    write_registry_db(paths, updated_registry_db)
    write_inactive_shards(
        paths,
        tracked_modules=current_tracked_modules,
    )
    remove_session_artifacts(paths)
    previous_decl_names = {
        str(existing.get("decl_name"))
        for existing in registry_db.get("proved_theorems", [])
        if isinstance(existing, dict)
    }
    new_decl_names = {
        str(entry["decl_name"])
        for entry in updated_registry_db.get("proved_theorems", [])
        if isinstance(entry, dict) and str(entry.get("decl_name")) not in previous_decl_names
    }
    print("Cleaned up the active theorem-registry session.")
    print(f"- tracked modules: {len(current_tracked_modules)}")
    print(f"- newly registered proved theorems: {len(new_decl_names)}")
    print(f"- proved registry DB: {relative_path_str(paths.project_root, paths.registry_db_file)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare and clean a single theorem-registry proof session."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="Freeze one target theorem into a prepared session.")
    prepare.add_argument(
        "--target",
        required=True,
        help="Target spec. Accepts either `<module>:<short-decl-or-fully-qualified-decl>` or a fully qualified declaration name.",
    )
    prepare.add_argument(
        "--base-commit",
        help="Optional base commit note. Defaults to the current HEAD when available.",
    )
    prepare.add_argument(
        "--auto-build",
        action="store_true",
        help="Automatically refresh stale or missing module artifacts before resolving the target or refreshing changed tracked modules.",
    )

    subparsers.add_parser("cleanup", help="Deactivate the current session and update the persistent proved theorem registry.")
    return parser


def main(*, project_root: Path | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args()
    root = project_root if project_root is not None else Path(__file__).resolve().parents[2]
    paths = make_paths(root.resolve())
    if args.command == "prepare":
        prepare_session(args, paths)
    elif args.command == "cleanup":
        cleanup_session(paths)
    else:
        fail("不支持的命令。", details=[f"命令：`{args.command}`"])
