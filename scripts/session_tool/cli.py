from __future__ import annotations

import argparse
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .common import (
    IMPORT_RE,
    MANAGED_ATTR_PREFIX,
    MANAGED_IMPORT_MARKER,
    TOOL_TEMPORARY_AXIOM_MODULE,
    acquire_prepare_lock,
    ensure_layout,
    fail,
    is_host_project_module,
    make_paths,
    module_name_to_path,
    module_name_to_relative_path,
    read_json,
    release_prepare_lock,
    write_json,
)
from .lean_ops import (
    build_module,
    ensure_probe_tool_ready,
    generated_runtime_source,
    module_artifact_path,
    probe_named_declarations_with_imports,
    reset_generated_runtime,
    try_git_head,
    try_probe_decl_in_module,
    write_generated_runtime,
)


SORRY_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_'])sorry(?![A-Za-z0-9_'])")
INLINE_MODULE_LIST_LIMIT = 10
INLINE_PERMITTED_AXIOM_LIMIT = 12


@dataclass(frozen=True)
class SessionInspection:
    session_exists: bool
    session_payload: dict[str, Any] | None
    runtime_matches_session: bool
    runtime_is_reset: bool
    import_files: set[str]
    attribute_markers: dict[str, set[str]]


@dataclass(frozen=True)
class TargetSpec:
    module_name: str | None
    decl_name: str


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
    if sep != "":
        module_name = module_name.strip()
        decl_part = decl_part.strip()
        if not module_name or not decl_part or ":" in decl_part:
            fail(
                "target 参数格式无效。",
                details=[
                    f"收到：`{target_spec}`",
                ],
                hints=[
                    "使用 `--target <module>:<decl>` 或 `--target <fully-qualified-decl>`。",
                    "例如：`--target MyProj.Section:goal`、`--target MyProj.Section:MyProj.Section.goal`、`--target MyProj.Section.goal`。",
                ],
            )
        decl_name = decl_part if "." in decl_part else f"{module_name}.{decl_part}"
        return TargetSpec(
            module_name=module_name,
            decl_name=decl_name,
        )
    decl_name = target_spec.strip()
    if not decl_name or ":" in decl_name or "." not in decl_name:
        fail(
            "target 参数格式无效。",
            details=[
                f"收到：`{target_spec}`",
            ],
            hints=[
                "使用 `--target <module>:<decl>` 或 `--target <fully-qualified-decl>`。",
                "例如：`--target MyProj.Section:goal`、`--target MyProj.Section:MyProj.Section.goal`、`--target MyProj.Section.goal`。",
            ],
        )
    return TargetSpec(
        module_name=None,
        decl_name=decl_name,
    )


def candidate_target_modules_from_decl_name(paths, decl_name: str) -> list[str]:
    parts = decl_name.split(".")
    candidates: list[str] = []
    for end in range(len(parts) - 1, 0, -1):
        module_name = ".".join(parts[:end])
        if is_host_project_module(paths.project_root, module_name) and module_name not in candidates:
            candidates.append(module_name)
    return candidates


def probe_target_decl(
    paths,
    *,
    decl_name: str,
    module_name: str,
    range_info: dict[str, int],
) -> dict[str, object]:
    payload = try_probe_decl_in_module(paths, module_name, decl_name)
    if payload is None:
        fail(
            "无法对目标声明执行 Lean probe。",
            details=[
                f"目标声明：`{decl_name}`",
                f"目标模块：`{module_name}`",
            ],
            hints=[
                "确认该声明在模块中可见，并且模块可以单独构建。",
            ],
        )
    resolved_module = str(payload["module"])
    if resolved_module != module_name:
        fail(
            "Lean probe 返回的模块与 `.ilean` 定位结果不一致。",
            details=[
                f"目标声明：`{decl_name}`",
                f"`.ilean` 定位模块：`{module_name}`",
                f"Lean probe 返回模块：`{resolved_module}`",
            ],
            hints=[
                "如果这是 re-export 场景，请直接使用定义该声明的模块。",
            ],
        )
    path = module_name_to_path(paths.project_root, module_name)
    return normalize_probed_decl(
        payload,
        module_name=module_name,
        relative_file=relative_path_str(paths.project_root, path),
        range_info=range_info,
    )


def summarize_attribute_markers(attribute_markers: dict[str, set[str]]) -> list[str]:
    lines: list[str] = []
    for relative_file in sorted(attribute_markers):
        decls = ", ".join(sorted(attribute_markers[relative_file]))
        lines.append(f"`{relative_file}`: {decls}")
    return lines


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
                "file": str(entry["file"]),
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
    module_closure = session_payload["freeze"]["module_closure"]
    base_commit = session_payload.get("base_commit")
    lines = [
        "TemporaryAxiomTool prepared session report",
        "",
        "Session summary",
        f"- target: {target['decl_name']}",
        f"- target module: {target['module']}",
        f"- target statement hash: {target['statement_hash']}",
        f"- base commit: {base_commit if base_commit is not None else '<none>'}",
        f"- module closure size: {len(module_closure)}",
        f"- modules with permitted axioms: {len(grouped_permitted_axioms)}",
        f"- permitted axioms: {sum(len(entries) for entries in grouped_permitted_axioms.values())}",
        "",
        "Module closure",
    ]
    lines.extend(f"- {module_name}" for module_name in module_closure)
    lines.append("")
    lines.append("Permitted temporary axioms by module")
    if not grouped_permitted_axioms:
        lines.append("- <none>")
    else:
        for module_name, entries in grouped_permitted_axioms.items():
            lines.append(f"- {module_name} ({len(entries)})")
            for entry in entries:
                lines.append(f"  - {entry['decl_name']} [{entry['origin']}]")
    lines.append("")
    lines.append("Artifacts")
    lines.append("- session.json: freeze data for verifier/comparator and cleanup edit log")
    lines.append("- temporary_axiom_tool_session_report.txt: human-readable session summary")
    lines.append("- TemporaryAxiomTool/PreparedSession/Generated.lean: generated Lean runtime")
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
    module_closure: list[str],
    grouped_permitted_axioms: dict[str, list[dict[str, str]]],
) -> None:
    permitted_count = sum(len(entries) for entries in grouped_permitted_axioms.values())
    involved_modules_count = len(module_closure)
    permitted_modules_count = len(grouped_permitted_axioms)
    print(f"Prepared session for `{target_info['decl_name']}`.")
    print(f"- target module: {target_info['module']}")
    print(f"- involved modules: {involved_modules_count}")
    print(f"- modules with permitted temporary axioms: {permitted_modules_count}")
    print(f"- permitted temporary axioms: {permitted_count}")
    if involved_modules_count <= INLINE_MODULE_LIST_LIMIT:
        print("- involved module list:")
        for module_name in module_closure:
            print(f"  - {module_name}")
    if permitted_count <= INLINE_PERMITTED_AXIOM_LIMIT:
        print("- permitted temporary axioms by module:")
        if not grouped_permitted_axioms:
            print("  - <none>")
        else:
            for module_name, entries in grouped_permitted_axioms.items():
                print(f"  - {module_name} ({len(entries)})")
                for entry in entries:
                    print(f"    - {entry['decl_name']}")
    else:
        print("- permitted temporary axiom list is long; see the saved report for the full grouped list.")
    print(f"- session data: {relative_path_str(paths.project_root, paths.session_file)}")
    print(f"- human-readable report: {relative_path_str(paths.project_root, paths.report_file)}")


def ilean_path_for_module(paths, module_name: str) -> Path:
    return paths.lean_build_lib_root / module_name_to_relative_path(module_name).with_suffix(".ilean")


def olean_path_for_module(paths, module_name: str) -> Path:
    return module_artifact_path(paths, module_name, ".olean")


def clear_module_metadata_caches() -> None:
    load_ilean_metadata.cache_clear()
    module_decl_entries_from_ilean.cache_clear()


def direct_host_imports_from_metadata(paths, metadata: dict[str, Any]) -> list[str]:
    imports: list[str] = []
    for item in metadata.get("directImports", []):
        if not isinstance(item, list) or not item:
            continue
        imported = str(item[0])
        if is_host_project_module(paths.project_root, imported) and imported not in imports:
            imports.append(imported)
    return imports


def direct_host_imports_from_source(paths, module_name: str) -> list[str]:
    path = module_name_to_path(paths.project_root, module_name)
    text = path.read_text(encoding="utf-8")
    imports: list[str] = []
    in_block_comment = False
    for line in text.splitlines():
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
            if is_host_project_module(paths.project_root, imported) and imported not in imports:
                imports.append(imported)
    return imports


def ensure_module_artifacts(
    paths,
    module_name: str,
    *,
    _done: set[str] | None = None,
    _visiting: set[str] | None = None,
) -> None:
    done = _done if _done is not None else set()
    visiting = _visiting if _visiting is not None else set()
    if module_name in done or module_name in visiting:
        return
    visiting.add(module_name)
    source_path = module_name_to_path(paths.project_root, module_name)
    ilean_path = ilean_path_for_module(paths, module_name)
    olean_path = olean_path_for_module(paths, module_name)
    metadata: dict[str, Any] | None = None
    direct_imports: list[str] = []
    if ilean_path.exists() and olean_path.exists():
        metadata = read_json(ilean_path)
        direct_imports = direct_host_imports_from_metadata(paths, metadata)
        for imported in direct_imports:
            ensure_module_artifacts(paths, imported, _done=done, _visiting=visiting)
    needs_build = not ilean_path.exists() or not olean_path.exists()
    if not needs_build:
        artifact_mtime = min(ilean_path.stat().st_mtime_ns, olean_path.stat().st_mtime_ns)
        if source_path.stat().st_mtime_ns > artifact_mtime:
            needs_build = True
        elif direct_host_imports_from_source(paths, module_name) != direct_imports:
            needs_build = True
        else:
            for imported in direct_imports:
                imported_ilean = ilean_path_for_module(paths, imported)
                imported_olean = olean_path_for_module(paths, imported)
                imported_mtime = max(imported_ilean.stat().st_mtime_ns, imported_olean.stat().st_mtime_ns)
                if imported_mtime > artifact_mtime:
                    needs_build = True
                    break
    if needs_build:
        build_module(paths, module_name)
        clear_module_metadata_caches()
    done.add(module_name)
    visiting.remove(module_name)


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
            hints=[
                f"先确认模块可以单独构建：`lake build {module_name}`。",
            ],
        )
    return read_json(path)


def try_decl_range_from_ilean(paths, module_name: str, decl_name: str) -> dict[str, int] | None:
    metadata = load_ilean_metadata(paths, module_name)
    decls = metadata.get("decls", {})
    raw = decls.get(decl_name)
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) < 4:
        fail(
            "`.ilean` 里的 declaration range 格式异常。",
            details=[
                f"模块：`{module_name}`",
                f"声明：`{decl_name}`",
            ],
            hints=[
                f"重新构建模块后再试：`lake build {module_name}`。",
            ],
        )
    return normalize_range(raw)


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


def range_key(payload: dict[str, object]) -> tuple[int, int]:
    range_info = payload["range"]
    assert isinstance(range_info, dict)
    return (int(range_info["line"]), int(range_info["column"]))


def short_decl_name(decl_name: str) -> str:
    return decl_name.rsplit(".", 1)[-1]


def is_strictly_before(lhs: dict[str, object], rhs: dict[str, object]) -> bool:
    return range_key(lhs) < range_key(rhs)


def ensure_decl_range_matches_source(
    *,
    module_name: str,
    decl_name: str,
    relative_file: str,
    snippet: str,
) -> None:
    short_name = short_decl_name(decl_name)
    prefix = snippet[:240]
    if prefix.lstrip().startswith(short_name) or short_name in prefix:
        return
    fail(
        "`.ilean` 给出的声明范围与当前源码不一致。",
        details=[
            f"声明：`{decl_name}`",
            f"模块：`{module_name}`",
            f"文件：`{relative_file}`",
            f"源码片段起始：`{prefix.strip()[:120] or '<empty>'}`",
        ],
        hints=[
            "这通常说明源码和 `.ilean` / `.olean` 已经不同步。",
            f"先重新构建相关模块：`lake build {module_name}`。",
        ],
    )


def resolve_target_decl(paths, *, decl_name: str, module_name: str | None) -> dict[str, object]:
    if module_name is not None:
        if not is_host_project_module(paths.project_root, module_name):
            fail(
                "指定的目标模块不存在于当前项目中。",
                details=[
                    f"目标声明：`{decl_name}`",
                    f"目标模块：`{module_name}`",
                ],
                hints=[
                    "确认 `--target` 里模块部分传入的是项目内的 Lean 模块名。",
                ],
            )
        ensure_module_artifacts(paths, module_name)
        range_info = try_decl_range_from_ilean(paths, module_name, decl_name)
        if range_info is None:
            fail(
                "指定模块的 `.ilean` 中找不到目标声明。",
                details=[
                    f"目标声明：`{decl_name}`",
                    f"目标模块：`{module_name}`",
                ],
                hints=[
                    "这里的模块部分必须是声明的定义模块，而不只是 re-export 它的模块。",
                    "如果你只有完整声明名，也可以直接使用 `--target <fully-qualified-decl>`。",
                ],
            )
        target_path = module_name_to_path(paths.project_root, module_name)
        target_text = target_path.read_text(encoding="utf-8")
        ensure_decl_range_matches_source(
            module_name=module_name,
            decl_name=decl_name,
            relative_file=relative_path_str(paths.project_root, target_path),
            snippet=slice_text_for_decl(target_text, build_line_starts(target_text), range_info),
        )
        return probe_target_decl(
            paths,
            decl_name=decl_name,
            module_name=module_name,
            range_info=range_info,
        )

    candidate_modules = candidate_target_modules_from_decl_name(paths, decl_name)
    if not candidate_modules:
        fail(
            "无法从完整声明名推断候选模块。",
            details=[
                f"目标声明：`{decl_name}`",
            ],
            hints=[
                "如果该声明名与模块路径不对齐，请改用 `--target <module>:<decl>`。",
            ],
        )
    for candidate_module in candidate_modules:
        ensure_module_artifacts(paths, candidate_module)
        range_info = try_decl_range_from_ilean(paths, candidate_module, decl_name)
        if range_info is None:
            continue
        target_path = module_name_to_path(paths.project_root, candidate_module)
        target_text = target_path.read_text(encoding="utf-8")
        ensure_decl_range_matches_source(
            module_name=candidate_module,
            decl_name=decl_name,
            relative_file=relative_path_str(paths.project_root, target_path),
            snippet=slice_text_for_decl(target_text, build_line_starts(target_text), range_info),
        )
        return probe_target_decl(
            paths,
            decl_name=decl_name,
            module_name=candidate_module,
            range_info=range_info,
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


def compute_module_closure(paths, target_module: str) -> list[str]:
    queue: deque[str] = deque([target_module])
    seen: set[str] = set()
    ordered: list[str] = []
    while queue:
        module_name = queue.popleft()
        if module_name in seen:
            continue
        seen.add(module_name)
        ordered.append(module_name)
        metadata = load_ilean_metadata(paths, module_name)
        for imported in direct_host_imports_from_metadata(paths, metadata):
            queue.append(imported)
    return ordered


def collect_probed_declarations(
    paths,
    *,
    imports: list[str],
    candidates: list[dict[str, object]],
) -> list[dict[str, object]]:
    if not candidates:
        return []
    requested_decl_names = [str(entry["decl_name"]) for entry in candidates]
    raw_payloads = probe_named_declarations_with_imports(
        paths,
        imports=imports,
        decl_names=requested_decl_names,
        description="批量探测 prepared session 中的声明",
    )
    by_name = {str(payload["decl_name"]): payload for payload in raw_payloads}
    missing_decl_names = [
        decl_name for decl_name in requested_decl_names if decl_name not in by_name
    ]
    if missing_decl_names:
        preview = missing_decl_names[:10]
        details = [
            f"缺失数量：{len(missing_decl_names)}",
            "缺失声明：\n" + "\n".join(f"- {decl_name}" for decl_name in preview),
        ]
        if len(missing_decl_names) > len(preview):
            details.append(f"其余缺失条目：{len(missing_decl_names) - len(preview)} 个")
        fail(
            "Lean probe 没有返回全部候选声明。",
            details=details,
            hints=[
                "这通常说明当前源码、模块产物和导入环境不一致。",
                "先重新构建相关模块，再重新运行 `prepare`。",
            ],
        )
    normalized: list[dict[str, object]] = []
    for entry in candidates:
        decl_name = str(entry["decl_name"])
        payload = by_name.get(decl_name)
        if payload is None:
            continue
        payload_module = str(payload["module"])
        if payload_module != str(entry["module"]):
            fail(
                "Lean probe 返回的模块与 `.ilean` 记录不一致。",
                details=[
                    f"声明：`{decl_name}`",
                    f"`.ilean` 记录模块：`{entry['module']}`",
                    f"Lean probe 返回模块：`{payload_module}`",
                ],
            )
        normalized.append(
            normalize_probed_decl(
                payload,
                module_name=str(entry["module"]),
                relative_file=str(entry["file"]),
                range_info=entry["range"],
            )
    )
    return normalized


@lru_cache(maxsize=None)
def module_decl_entries_from_ilean(paths, module_name: str) -> list[dict[str, object]]:
    metadata = load_ilean_metadata(paths, module_name)
    decls = metadata.get("decls", {})
    if not isinstance(decls, dict):
        fail(
            "`.ilean` 里的 `decls` 字段格式异常。",
            details=[f"模块：`{module_name}`"],
        )
    path = module_name_to_path(paths.project_root, module_name)
    if not path.exists():
        fail(
            "模块元数据存在，但找不到对应源码文件。",
            details=[
                f"模块：`{module_name}`",
                f"期望源码：`{relative_path_str(paths.project_root, path)}`",
            ],
        )
    relative_file = relative_path_str(paths.project_root, path)
    entries: list[dict[str, object]] = []
    for decl_name, raw in decls.items():
        if not isinstance(decl_name, str):
            continue
        if not isinstance(raw, list) or len(raw) < 4:
            continue
        entries.append(
            {
                "decl_name": decl_name,
                "module": module_name,
                "file": relative_file,
                "range": normalize_range(raw),
            }
        )
    return sorted(
        entries,
        key=lambda item: (range_key(item), str(item["decl_name"])),
    )


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


def slice_text_for_decl(text: str, line_starts: list[int], range_info: dict[str, int]) -> str:
    start_line = int(range_info.get("selection_line", range_info["line"]))
    start_column = int(range_info.get("selection_column", range_info["column"]))
    end_line = int(range_info["end_line"])
    end_column = int(range_info["end_column"])
    start = offset_for_line_column(text, line_starts, start_line, start_column)
    end = offset_for_line_column(text, line_starts, end_line, end_column)
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


def decl_uses_explicit_sorry(snippet: str) -> bool:
    if "sorry" not in snippet:
        return False
    sanitized = strip_lean_comments_and_strings(snippet)
    return SORRY_TOKEN_RE.search(sanitized) is not None


def collect_permitted_axioms(paths, target_info: dict[str, object]) -> tuple[list[str], list[dict[str, object]]]:
    target_module = str(target_info["module"])
    closure = compute_module_closure(paths, target_module)
    target_decl = str(target_info["decl_name"])
    candidate_entries: list[dict[str, object]] = []
    for module_name in closure:
        entries = module_decl_entries_from_ilean(paths, module_name)
        if not entries:
            continue
        module_path = module_name_to_path(paths.project_root, module_name)
        source_text = module_path.read_text(encoding="utf-8")
        line_starts = build_line_starts(source_text)
        for entry in entries:
            if str(entry["decl_name"]) == target_decl:
                continue
            if module_name == target_module and not is_strictly_before(entry, target_info):
                break
            snippet = slice_text_for_decl(source_text, line_starts, entry["range"])
            ensure_decl_range_matches_source(
                module_name=module_name,
                decl_name=str(entry["decl_name"]),
                relative_file=str(entry["file"]),
                snippet=snippet,
            )
            if not decl_uses_explicit_sorry(snippet):
                continue
            candidate_entries.append(entry)
    permitted_by_name: dict[str, dict[str, object]] = {}
    for decl_info in collect_probed_declarations(paths, imports=closure, candidates=candidate_entries):
        module_name = str(decl_info["module"])
        permitted_by_name[str(decl_info["decl_name"])] = {
            "decl_name": str(decl_info["decl_name"]),
            "module": module_name,
            "file": str(decl_info["file"]),
            "statement_hash": str(decl_info["statement_hash"]),
            "origin": "prior_same_module" if module_name == target_module else "dependency_module",
            "range": decl_info["range"],
        }
    permitted_axioms = sorted(
        permitted_by_name.values(),
        key=lambda item: (
            str(item["file"]),
            int(item["range"]["line"]),
            int(item["range"]["column"]),
            str(item["decl_name"]),
        ),
    )
    return closure, permitted_axioms


def read_generated_runtime_text(paths) -> str:
    if not paths.generated_session_file.exists():
        return ""
    return paths.generated_session_file.read_text(encoding="utf-8")


def extract_session_target(session_payload: dict[str, Any]) -> dict[str, str]:
    try:
        target = session_payload["freeze"]["target"]
        decl_name = str(target["decl_name"])
        module_name = str(target["module"])
        statement_hash = str(target["statement_hash"])
    except (KeyError, TypeError):
        fail("活动 session 文件缺少 `freeze.target` 的必要字段。")
    return {
        "decl_name": decl_name,
        "module": module_name,
        "statement_hash": statement_hash,
    }


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
                    "statement_hash": str(item["statement_hash"]),
                }
            )
        except KeyError:
            fail("活动 session 文件中的 permitted axioms 条目缺少必要字段。")
    return entries


def expected_runtime_from_session(session_payload: dict[str, Any]) -> str:
    target = extract_session_target(session_payload)
    permitted_axioms = extract_session_permitted_axioms(session_payload)
    return generated_runtime_source(
        target_decl=target["decl_name"],
        permitted_axioms=permitted_axioms,
    )


def scan_managed_markers_in_files(
    paths,
    relative_files: set[str],
) -> tuple[set[str], dict[str, set[str]]]:
    import_files: set[str] = set()
    attribute_markers: dict[str, set[str]] = defaultdict(set)
    for relative_file in sorted(relative_files):
        path = paths.project_root / relative_file
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line in text.splitlines():
            if MANAGED_IMPORT_MARKER in line:
                import_files.add(relative_file)
            if MANAGED_ATTR_PREFIX in line:
                decl_name = line.partition(f"{MANAGED_ATTR_PREFIX} ")[2].strip()
                if decl_name:
                    attribute_markers[relative_file].add(decl_name)
    return import_files, dict(attribute_markers)


def inspect_session_state(paths) -> SessionInspection:
    session_exists = paths.session_file.exists()
    session_payload = read_json(paths.session_file) if session_exists else None
    runtime_text = read_generated_runtime_text(paths)
    reset_runtime = generated_runtime_source(target_decl=None, permitted_axioms=[])
    runtime_is_reset = runtime_text == reset_runtime
    runtime_matches_session = False
    import_files: set[str] = set()
    attribute_markers: dict[str, set[str]] = {}
    if session_payload is not None:
        runtime_matches_session = runtime_text == expected_runtime_from_session(session_payload)
        expected_imports, expected_attrs = expected_markers_from_session(session_payload)
        tracked_files = expected_imports | set(expected_attrs)
        import_files, attribute_markers = scan_managed_markers_in_files(paths, tracked_files)
    return SessionInspection(
        session_exists=session_exists,
        session_payload=session_payload,
        runtime_matches_session=runtime_matches_session,
        runtime_is_reset=runtime_is_reset,
        import_files=import_files,
        attribute_markers=attribute_markers,
    )


def ensure_no_stale_workspace_state(paths, inspection: SessionInspection) -> None:
    if inspection.session_exists:
        return
    details: list[str] = []
    if not inspection.runtime_is_reset:
        details.append(
            f"生成 runtime 没有处于重置状态：`{relative_path_str(paths.project_root, paths.generated_session_file)}`"
        )
    if details:
        fail(
            "没有活动 session 文件，但 workspace 里仍然残留上一次 session 的状态。",
            details=details,
            hints=[
                "先手动检查 generated runtime 是否应当重置。",
                "必要时恢复正确的 `session.json` 后再执行 `cleanup`，或手动整理当前 workspace。",
            ],
        )


def expected_markers_from_session(session_payload: dict[str, Any]) -> tuple[set[str], dict[str, set[str]]]:
    try:
        edits = session_payload["cleanup"]["edits"]
    except (KeyError, TypeError):
        fail("活动 session 文件缺少 `cleanup.edits` 字段。")
    raw_imports = edits.get("imports", [])
    raw_attributes = edits.get("attributes", [])
    if not isinstance(raw_imports, list) or not isinstance(raw_attributes, list):
        fail("活动 session 文件中的 `cleanup.edits` 格式无效。")
    import_files: set[str] = set()
    attribute_markers: dict[str, set[str]] = defaultdict(set)
    for item in raw_imports:
        if isinstance(item, dict) and "file" in item:
            import_files.add(str(item["file"]))
    for item in raw_attributes:
        if isinstance(item, dict) and "file" in item and "decl_name" in item:
            attribute_markers[str(item["file"])].add(str(item["decl_name"]))
    return import_files, dict(attribute_markers)


def ensure_active_session_consistent(paths, inspection: SessionInspection) -> dict[str, Any]:
    session_payload = inspection.session_payload
    if session_payload is None:
        fail("内部错误：缺少活动 session payload。")
    expected_imports, expected_attrs = expected_markers_from_session(session_payload)
    missing_imports = sorted(expected_imports - inspection.import_files)
    extra_imports = sorted(inspection.import_files - expected_imports)
    missing_attrs: dict[str, set[str]] = {}
    extra_attrs: dict[str, set[str]] = {}
    all_attr_files = set(expected_attrs) | set(inspection.attribute_markers)
    for relative_file in sorted(all_attr_files):
        expected = expected_attrs.get(relative_file, set())
        actual = inspection.attribute_markers.get(relative_file, set())
        if expected - actual:
            missing_attrs[relative_file] = expected - actual
        if actual - expected:
            extra_attrs[relative_file] = actual - expected
    details: list[str] = []
    target = extract_session_target(session_payload)
    details.append(f"当前活动 target：`{target['decl_name']}`")
    if not inspection.runtime_matches_session:
        details.append(
            f"`{relative_path_str(paths.project_root, paths.generated_session_file)}` 与活动 session 不一致。"
        )
    if missing_imports:
        details.append("缺失的 managed import：\n" + "\n".join(f"- `{file}`" for file in missing_imports))
    if extra_imports:
        details.append("额外的 managed import：\n" + "\n".join(f"- `{file}`" for file in extra_imports))
    if missing_attrs:
        details.append(
            "缺失的 managed attribute：\n" + "\n".join(
                f"- {line}" for line in summarize_attribute_markers(missing_attrs)
            )
        )
    if extra_attrs:
        details.append(
            "额外的 managed attribute：\n" + "\n".join(
                f"- {line}" for line in summarize_attribute_markers(extra_attrs)
            )
        )
    if len(details) > 1:
        fail(
            "检测到活动 session，但 workspace 状态与 session manifest 不一致。",
            details=details,
            hints=[
                "优先尝试运行 `python3 scripts/temporary_axiom_session.py cleanup` 回滚 managed 修改。",
                "如果源码已经被手动改动，请先检查 `session.json`、generated runtime 和相关模块文件。",
            ],
        )
    return session_payload


def ensure_file_is_not_already_managed(text: str, *, file_label: str) -> None:
    if MANAGED_IMPORT_MARKER in text or MANAGED_ATTR_PREFIX in text:
        fail(
            "检测到未清理的 managed 标记，不能重复 prepare。",
            details=[f"文件：`{file_label}`"],
            hints=[
                "先运行 `python3 scripts/temporary_axiom_session.py cleanup`。",
                "如果上一次 session 被中断，先检查源码后再手动清理残留标记。",
            ],
        )


def attr_marker_line(decl_name: str) -> str:
    return f"@[temporary_axiom] {MANAGED_ATTR_PREFIX} {decl_name}"


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


def has_tool_import(lines: list[str]) -> bool:
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"import {TOOL_TEMPORARY_AXIOM_MODULE}"):
            return True
    return False


def add_managed_import(lines: list[str]) -> bool:
    if has_tool_import(lines):
        return False
    insert_idx = compute_import_insertion_index(lines)
    lines.insert(insert_idx, f"import {TOOL_TEMPORARY_AXIOM_MODULE} {MANAGED_IMPORT_MARKER}")
    return True


def add_managed_attributes(lines: list[str], file_entries: list[dict[str, object]]) -> list[dict[str, str]]:
    inserted_for: list[dict[str, str]] = []
    for entry in sorted(
        file_entries,
        key=lambda item: (
            int(item["range"]["line"]),
            int(item["range"]["column"]),
            str(item["decl_name"]),
        ),
        reverse=True,
    ):
        decl_name = str(entry["decl_name"])
        marker_line = attr_marker_line(decl_name)
        insertion_line = int(entry["range"].get("selection_line", entry["range"]["line"]))
        start_idx = insertion_line - 1
        if start_idx < 0 or start_idx > len(lines):
            fail(
                "声明的源码范围超出文件边界。",
                details=[
                    f"声明：`{decl_name}`",
                    f"插入行：{insertion_line}",
                ],
                hints=[
                    "这通常说明源码和 `.ilean` 不一致；请先重新构建相关模块。",
                ],
            )
        lower = max(0, start_idx - 2)
        if any("temporary_axiom" in line for line in lines[lower:start_idx + 1]):
            fail(
                "声明附近已经存在 `temporary_axiom` 标记。",
                details=[f"声明：`{decl_name}`"],
                hints=[
                    "先清理已有标签，再重新运行 `prepare`。",
                    "如果这是手工写入的标签，请先确认它是否应由本次 session 接管。",
                ],
            )
        if start_idx < len(lines) and lines[start_idx].lstrip().startswith("@["):
            current = lines[start_idx]
            if "--" in current:
                fail(
                    "attribute 行带有尾注释，当前版本无法安全改写。",
                    details=[
                        f"声明：`{decl_name}`",
                        f"原始行：`{current.strip()}`",
                    ],
                    hints=[
                        "把注释移到上一行，或手动整理 attribute 行后再运行 `prepare`。",
                    ],
                )
            lines[start_idx] = current.replace("@[", "@[temporary_axiom, ", 1) + f" {MANAGED_ATTR_PREFIX} {decl_name}"
            inserted_for.append({"decl_name": decl_name, "mode": "patched_existing_attr"})
        else:
            lines.insert(start_idx, marker_line)
            inserted_for.append({"decl_name": decl_name, "mode": "inserted_line"})
    inserted_for.reverse()
    return inserted_for


def write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def apply_prepare_edits(paths, permitted_axioms: list[dict[str, object]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for entry in permitted_axioms:
        grouped[str(entry["file"])].append(entry)
    originals: dict[Path, str] = {}
    edits = {"imports": [], "attributes": []}
    try:
        for relative_file, file_entries in sorted(grouped.items()):
            path = paths.project_root / relative_file
            if not path.exists():
                fail(
                    "prepare 过程中找不到待修改的源码文件。",
                    details=[f"文件：`{relative_file}`"],
                    hints=[
                        "确认在收集 permitted declarations 之后，没有移动或删除该文件。",
                    ],
                )
            original_text = path.read_text(encoding="utf-8")
            originals[path] = original_text
            ensure_file_is_not_already_managed(original_text, file_label=relative_file)
            lines = original_text.splitlines()
            inserted_decls = add_managed_attributes(lines, file_entries)
            if add_managed_import(lines):
                edits["imports"].append({"file": relative_file})
            for attr_edit in inserted_decls:
                edits["attributes"].append(
                    {
                        "decl_name": attr_edit["decl_name"],
                        "file": relative_file,
                        "mode": attr_edit["mode"],
                    }
                )
            write_lines(path, lines)
    except Exception:
        for path, original_text in originals.items():
            path.write_text(original_text, encoding="utf-8")
        raise
    return edits


def load_session(paths) -> dict[str, Any]:
    if not paths.session_file.exists():
        inspection = inspect_session_state(paths)
        ensure_no_stale_workspace_state(paths, inspection)
        fail(
            "当前没有可清理的活动 session。",
            details=[
                f"期望的 session 文件：`{relative_path_str(paths.project_root, paths.session_file)}`",
            ],
            hints=[
                "先运行 `prepare`，或确认上一次 session 是否已经被 cleanup。",
            ],
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


def fail_existing_session_prepare(
    paths,
    *,
    requested_target: str,
    requested_hash: str,
    session_payload: dict[str, Any],
) -> None:
    active_target = extract_session_target(session_payload)
    if requested_target != active_target["decl_name"]:
        fail(
            "已有活动 session，不能切换到另一个 target。",
            details=[
                f"当前活动 target：`{active_target['decl_name']}`",
                f"当前活动 target hash：`{active_target['statement_hash']}`",
                f"本次请求 target：`{requested_target}`",
                f"本次请求 target hash：`{requested_hash}`",
            ],
            hints=[
                "继续当前 target 的证明，或先执行 `cleanup` 再为新 target 重新 `prepare`。",
            ],
        )
    if requested_hash != active_target["statement_hash"]:
        fail(
            "已有活动 session，且同一个 target 的 statement hash 已发生变化。",
            details=[
                f"target：`{requested_target}`",
                f"活动 session 冻结 hash：`{active_target['statement_hash']}`",
                f"当前源码解析出的 hash：`{requested_hash}`",
            ],
            hints=[
                "先检查这次 statement 改动是否有意为之。",
                "如果需要刷新冻结结果，先执行 `cleanup`，再重新运行 `prepare`。",
            ],
        )
    fail(
        "已有活动 session，且同一个 target 已经以相同 statement hash 冻结。",
        details=[
            f"target：`{requested_target}`",
            f"statement hash：`{requested_hash}`",
        ],
        hints=[
            "直接继续当前 proving attempt。",
            "只有在需要重置 workspace 时，才先执行 `cleanup` 再重新 `prepare`。",
        ],
    )


def prepare_session(args: argparse.Namespace, paths) -> None:
    acquire_prepare_lock(paths)
    try:
        requested_target = parse_target_spec(args.target)
        inspection = inspect_session_state(paths)
        if inspection.session_exists:
            session_payload = ensure_active_session_consistent(paths, inspection)
            ensure_probe_tool_ready(paths)
            requested_target_info = resolve_target_decl(
                paths,
                decl_name=requested_target.decl_name,
                module_name=requested_target.module_name,
            )
            fail_existing_session_prepare(
                paths,
                requested_target=str(requested_target_info["decl_name"]),
                requested_hash=str(requested_target_info["statement_hash"]),
                session_payload=session_payload,
            )
        ensure_no_stale_workspace_state(paths, inspection)
        ensure_probe_tool_ready(paths)
        target_info = resolve_target_decl(
            paths,
            decl_name=requested_target.decl_name,
            module_name=requested_target.module_name,
        )
        module_closure, permitted_axioms = collect_permitted_axioms(paths, target_info)
        base_commit = args.base_commit if args.base_commit is not None else try_git_head(paths)
        write_generated_runtime(
            paths,
            target_decl=str(target_info["decl_name"]),
            permitted_axioms=permitted_axioms,
        )
        try:
            edits = apply_prepare_edits(paths, permitted_axioms)
        except Exception:
            reset_generated_runtime(paths)
            raise
        try:
            session_payload = {
                "schema_version": 2,
                "base_commit": base_commit,
                "freeze": {
                    "target": {
                        "decl_name": str(target_info["decl_name"]),
                        "module": str(target_info["module"]),
                        "statement_hash": str(target_info["statement_hash"]),
                    },
                    "module_closure": module_closure,
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
                "cleanup": {
                    "edits": edits,
                },
            }
            write_session(paths, session_payload)
            grouped_permitted_axioms = write_prepare_reports(
                paths,
                session_payload=session_payload,
                permitted_axioms=permitted_axioms,
            )
            ensure_active_session_consistent(paths, inspect_session_state(paths))
        except BaseException:
            cleanup_session_artifacts(paths, edits)
            reset_generated_runtime(paths)
            remove_session_artifacts(paths)
            raise
        print_prepare_summary(
            paths,
            target_info=target_info,
            module_closure=module_closure,
            grouped_permitted_axioms=grouped_permitted_axioms,
        )
    finally:
        release_prepare_lock(paths)


def cleanup_session_artifacts(paths, edits: dict[str, Any]) -> None:
    import_files = {str(item["file"]) for item in edits.get("imports", [])}
    attr_modes_by_file: dict[str, dict[str, str]] = defaultdict(dict)
    for item in edits.get("attributes", []):
        attr_modes_by_file[str(item["file"])][str(item["decl_name"])] = str(item["mode"])
    files = sorted(import_files | set(attr_modes_by_file))
    for relative_file in files:
        path = paths.project_root / relative_file
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        updated: list[str] = []
        attr_modes = attr_modes_by_file.get(relative_file, {})
        for line in lines:
            if MANAGED_IMPORT_MARKER in line:
                continue
            if MANAGED_ATTR_PREFIX not in line:
                updated.append(line)
                continue
            decl_name = line.partition(f"{MANAGED_ATTR_PREFIX} ")[2].strip()
            mode = attr_modes.get(decl_name)
            if mode is None:
                updated.append(line)
                continue
            if mode == "inserted_line":
                continue
            cleaned = line.replace(f" {MANAGED_ATTR_PREFIX} {decl_name}", "")
            cleaned = cleaned.replace("@[temporary_axiom, ", "@[", 1)
            updated.append(cleaned)
        if updated != lines:
            write_lines(path, updated)


def cleanup_session(paths) -> None:
    session = load_session(paths)
    edits = session.get("cleanup", {}).get("edits", {})
    cleanup_session_artifacts(paths, edits)
    reset_generated_runtime(paths)
    remove_session_artifacts(paths)
    print("Cleaned up the active temporary-axiom session.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare and clean a single temporary-axiom proof session."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="Freeze one target theorem into a prepared session.")
    prepare.add_argument(
        "--target",
        required=True,
        help="Target spec. Accepts either `<module>:<decl>` or a fully qualified declaration name.",
    )
    prepare.add_argument(
        "--base-commit",
        help="Optional base commit note. Defaults to the current HEAD when available.",
    )

    subparsers.add_parser("cleanup", help="Remove the active session's managed edits and generated files.")
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
        fail(
            "不支持的命令。",
            details=[f"命令：`{args.command}`"],
        )
