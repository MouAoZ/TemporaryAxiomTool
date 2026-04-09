from __future__ import annotations

import argparse
import os
import re
from concurrent.futures import ThreadPoolExecutor
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
    compute_text_file_hashes,
    compute_text_hashes_for_texts,
    ensure_probe_tool_ready,
    generated_runtime_source,
    module_artifact_path,
    probe_named_declarations_with_imports,
    reset_generated_runtime,
    run_command,
    run_lean_source,
    try_git_head,
    try_probe_decl_in_module,
    write_generated_runtime,
)


SORRY_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_'])sorry(?![A-Za-z0-9_'])")
LEAN_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_']*")
THEOREM_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_'])theorem(?![A-Za-z0-9_'])")
TEMP_AXIOM_HASH_MISMATCH_RE = re.compile(
    r"Invalid @\[temporary_axiom\] target (?P<decl>[^\n]+)\n\n"
    r"The frozen prepared-session entry does not match the elaborated statement\.\n\n"
    r"Expected hash: (?P<expected>\d+)\n\n"
    r"Actual hash:\s+(?P<actual>\d+)"
)
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

AXIOM_PROBE_HELPER_SOURCE = """open Lean Elab Command Term

private meta def temporaryAxiomProbeJson
    (declName : Name)
    (moduleName : Name)
    (statementHash : UInt64) : Json :=
  Json.mkObj [
    ("decl_name", Json.str <| toString declName),
    ("module", Json.str <| toString moduleName),
    ("statement_hash", Json.str <| toString statementHash.toNat)
  ]

private meta def isSupportedTemporaryAxiomProbeConstInfo (constInfo : ConstantInfo) : Bool :=
  match constInfo with
  | .thmInfo _ => true
  | .opaqueInfo _ => true
  | .axiomInfo _ => true
  | _ => false

syntax (name := Parser.Attr.temporary_axiom) "temporary_axiom" : attr

builtin_initialize registerBuiltinAttribute {
    name := `temporary_axiom
    descr := "temporary axiom probe attribute"
    applicationTime := AttributeApplicationTime.afterTypeChecking
    add := fun _declName _stx _kind => pure ()
    erase := fun _declName => pure ()
  }

private meta def isTemporaryAxiomAttr (attrInstance : Syntax) : Bool :=
  if attrInstance.getKind != ``Parser.Term.attrInstance then
    false
  else
    let attr := attrInstance[1]
    attr.getKind == ``Parser.Attr.temporary_axiom ||
      (attr.getKind == ``Parser.Attr.simple &&
        attr[0].getId.eraseMacroScopes == `temporary_axiom &&
        attr[1].isNone &&
        attr[2].isNone)

private meta def hasTemporaryAxiomAttr (modifiers : Syntax) : Bool :=
  if modifiers.getKind != ``Parser.Command.declModifiers then
    false
  else
    let attrsOpt := modifiers[1]
    if attrsOpt.isNone then
      false
    else
      let attrs := attrsOpt[0][1].getSepArgs
      attrs.any isTemporaryAxiomAttr

macro_rules
  (kind := Lean.Parser.Command.declaration)
  | `($modifiers:declModifiers theorem $declId:declId $declSig:declSig $_:declVal) => do
      if !hasTemporaryAxiomAttr modifiers then
        Macro.throwUnsupported
      `(command| $modifiers:declModifiers axiom $declId:declId $declSig:declSig)

elab "#print_axiomized_decl_probe " quotedName:term " in " quotedModule:term : command => do
  let declName ← liftTermElabM do
    unsafe Term.evalTerm Name (mkConst ``Name) quotedName
  let moduleName ← liftTermElabM do
    unsafe Term.evalTerm Name (mkConst ``Name) quotedModule
  let env ← getEnv
  let some constInfo := env.find? declName | pure ()
  if isSupportedTemporaryAxiomProbeConstInfo constInfo then
    liftIO <| IO.println <|
      (temporaryAxiomProbeJson
        declName
        moduleName
        (TemporaryAxiomTool.statementHashOfConstInfo constInfo)).compress
"""


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
    decl_is_short_name: bool


@dataclass(frozen=True)
class ArtifactIssue:
    module_name: str
    detail: str


def relative_path_str(project_root: Path, path: Path) -> str:
    return path.resolve().relative_to(project_root.resolve()).as_posix()


def text_contains_managed_markers(text: str) -> bool:
    return MANAGED_IMPORT_MARKER in text or MANAGED_ATTR_PREFIX in text


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
            details=[
                f"收到：`{target_spec}`",
            ],
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
    verified: bool,
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
    if verified:
        print("- hash verification: completed during prepare")
    else:
        print("- hash verification: skipped by `--no-verify`; later `lake build` may still expose mismatch")
    print(f"- session data: {relative_path_str(paths.project_root, paths.session_file)}")
    print(f"- human-readable report: {relative_path_str(paths.project_root, paths.report_file)}")


def ilean_path_for_module(paths, module_name: str) -> Path:
    return paths.lean_build_lib_root / module_name_to_relative_path(module_name).with_suffix(".ilean")


def olean_path_for_module(paths, module_name: str) -> Path:
    return module_artifact_path(paths, module_name, ".olean")


def trace_path_for_module(paths, module_name: str) -> Path:
    return module_artifact_path(paths, module_name, ".trace")


@lru_cache(maxsize=None)
def module_source_text(paths, module_name: str) -> str:
    path = module_name_to_path(paths.project_root, module_name)
    return path.read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def normalized_module_source_text(paths, module_name: str) -> str:
    return normalized_module_source_result(paths, module_name)[0]


@lru_cache(maxsize=None)
def normalized_module_source_result(paths, module_name: str) -> tuple[str, bool]:
    text = module_source_text(paths, module_name)
    if not text_contains_managed_markers(text):
        return text, False
    return normalize_managed_text(text)


@lru_cache(maxsize=None)
def normalized_module_source_line_starts(paths, module_name: str) -> list[int]:
    return build_line_starts(normalized_module_source_text(paths, module_name))


@lru_cache(maxsize=None)
def sanitized_module_source_text(paths, module_name: str) -> str:
    return strip_lean_comments_and_strings(normalized_module_source_text(paths, module_name))


@lru_cache(maxsize=None)
def module_relative_file(paths, module_name: str) -> str:
    path = module_name_to_path(paths.project_root, module_name)
    return relative_path_str(paths.project_root, path)


def clear_module_metadata_caches() -> None:
    load_ilean_metadata.cache_clear()
    module_decl_entries_from_ilean.cache_clear()
    normalized_module_source_result.cache_clear()
    normalized_module_source_text.cache_clear()
    normalized_module_source_line_starts.cache_clear()
    sanitized_module_source_text.cache_clear()


def direct_host_imports_from_metadata(paths, metadata: dict[str, Any]) -> list[str]:
    imports: list[str] = []
    for item in metadata.get("directImports", []):
        if not isinstance(item, list) or not item:
            continue
        imported = str(item[0])
        if is_host_project_module(paths.project_root, imported) and imported not in imports:
            imports.append(imported)
    return imports


@lru_cache(maxsize=None)
def direct_host_imports_from_source(paths, module_name: str) -> tuple[str, ...]:
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
            if MANAGED_IMPORT_MARKER in line:
                continue
            match = IMPORT_RE.match(line)
            if match is None:
                break
            for imported in match.group("mods").split():
                if is_host_project_module(paths.project_root, imported) and imported not in imports:
                    imports.append(imported)
    return tuple(imports)


def ensure_module_artifacts(
    paths,
    module_name: str,
) -> None:
    build_module(paths, module_name)
    clear_module_metadata_caches()


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
            normalized_caption = caption.replace("\\", "/")
            caption_matches = normalized_caption.endswith(relative_source_suffix)
        if not caption_matches:
            continue
        normalized = value.lower()
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
    raw_hash_paths: list[Path] = []
    normalized_hash_texts: dict[Path, str] = {}
    for current_module in modules_requiring_hash_check:
        source_path = module_name_to_path(paths.project_root, current_module).resolve()
        normalized_text, was_normalized = normalized_module_source_result(paths, current_module)
        if was_normalized:
            normalized_hash_texts[source_path] = normalized_text
        else:
            raw_hash_paths.append(source_path)
    current_source_hashes = compute_text_file_hashes(paths, raw_hash_paths)
    current_source_hashes.update(compute_text_hashes_for_texts(paths, normalized_hash_texts))
    for current_module in modules_requiring_hash_check:
        source_path = module_name_to_path(paths.project_root, current_module).resolve()
        current_hash = current_source_hashes.get(source_path)
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
        closure, _ = source_host_module_closure(
            paths,
            root_module,
            imports_cache=imports_cache,
        )
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


def collect_artifact_issues(
    paths,
    module_name: str,
) -> list[ArtifactIssue]:
    root_closures, issue_by_module = collect_artifact_issue_state_for_roots(paths, [module_name])
    return artifact_issues_for_closure(root_closures[module_name], issue_by_module)


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
) -> None:
    unique_issues = dedupe_artifact_issues(issues)
    hints = []
    if len(root_modules) == 1:
        hints.append(f"先运行 `lake build {root_modules[0]}`，或直接运行 `lake build`。")
    else:
        hints.append("先运行 `lake build`，确保候选目标模块及其依赖产物都是最新的。")
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
        )
    print("Detected missing or out-of-sync module artifacts before prepare.", flush=True)
    print(f"- root module: {root_module}", flush=True)
    print(f"- modules needing refresh: {len(issues)}", flush=True)
    print(format_artifact_issue_preview(issues), flush=True)
    print("- refreshing the root module via `lake build` because `--auto-build` was set...", flush=True)
    ensure_module_artifacts(paths, root_module)
    refreshed_closure, _ = source_host_module_closure(paths, root_module)
    return refreshed_closure


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


def resolve_decl_reference_in_module(
    paths,
    *,
    module_name: str,
    decl_name: str,
    decl_is_short_name: bool,
) -> tuple[str, dict[str, int]]:
    if not decl_is_short_name:
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
        return decl_name, range_info

    matches = [
        entry
        for entry in module_decl_entries_from_ilean(paths, module_name)
        if short_decl_name(str(entry["decl_name"])) == decl_name
    ]
    if not matches:
        fail(
            "指定模块中找不到该短名对应的目标声明。",
            details=[
                f"目标短名：`{decl_name}`",
                f"目标模块：`{module_name}`",
            ],
            hints=[
                "如果该声明名在 Lean 里带额外 namespace，可以改用 `--target <module>:<fully-qualified-decl>`。",
            ],
        )
    if len(matches) > 1:
        candidates = "\n".join(f"- {entry['decl_name']}" for entry in matches[:10])
        details = [
            f"目标短名：`{decl_name}`",
            f"目标模块：`{module_name}`",
            "匹配到多个声明：\n" + candidates,
        ]
        if len(matches) > 10:
            details.append(f"其余候选：{len(matches) - 10} 个")
        fail(
            "指定模块中的目标短名不唯一。",
            details=details,
            hints=[
                "请改用 `--target <module>:<fully-qualified-decl>` 明确指定目标声明。",
            ],
        )
    match = matches[0]
    return str(match["decl_name"]), match["range"]


def is_strictly_before(lhs: dict[str, object], rhs: dict[str, object]) -> bool:
    return range_key(lhs) < range_key(rhs)


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
                details=[
                    f"目标声明：`{decl_name}`",
                    f"目标模块：`{module_name}`",
                ],
                hints=[
                    "确认 `--target` 里模块部分传入的是项目内的 Lean 模块名。",
                ],
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
        candidate_modules
        if candidate_modules is not None
        else candidate_target_modules_from_decl_name(paths, decl_name)
    )
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
        range_info = try_decl_range_from_ilean(paths, candidate_module, decl_name)
        if range_info is None:
            continue
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


def dependency_first_module_order(paths, modules: list[str]) -> list[str]:
    module_set = set(modules)
    visiting: set[str] = set()
    visited: set[str] = set()
    ordered: list[str] = []

    def visit(module_name: str) -> None:
        if module_name in visited:
            return
        if module_name in visiting:
            fail(
                "模块依赖图存在循环，无法为 `verify` 计算稳定的构建顺序。",
                details=[f"涉及模块：`{module_name}`"],
            )
        visiting.add(module_name)
        metadata = load_ilean_metadata(paths, module_name)
        for imported in direct_host_imports_from_metadata(paths, metadata):
            if imported in module_set:
                visit(imported)
        visiting.remove(module_name)
        visited.add(module_name)
        ordered.append(module_name)

    for module_name in modules:
        visit(module_name)
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


def build_axiom_probe_source(
    *,
    prefix_source: str,
    file_entries: list[dict[str, object]],
) -> str:
    lines = prefix_source.splitlines()
    add_managed_attributes(
        text=prefix_source,
        lines=lines,
        file_entries=file_entries,
    )
    insert_idx = compute_import_insertion_index(lines)
    source_lines: list[str] = []
    source_lines.extend(lines[:insert_idx])
    source_lines.append("public import Lean.Attributes")
    source_lines.append("public meta import TemporaryAxiomTool.StatementHash")
    source_lines.append("public meta import Lean.Attributes")
    source_lines.append("public meta import Lean.Elab.Command")
    source_lines.append("public meta import Lean.Elab.Term")
    source_lines.append("")
    source_lines.extend(AXIOM_PROBE_HELPER_SOURCE.splitlines())
    source_lines.append("")
    source_lines.extend(lines[insert_idx:])
    source_lines.append("")
    for entry in file_entries:
        source_lines.append(
            f"#print_axiomized_decl_probe `{entry['decl_name']} in `{entry['module']}"
        )
    return "\n".join(source_lines) + "\n"


def collect_axiomized_probed_declarations(
    paths,
    *,
    candidates: list[dict[str, object]],
) -> list[dict[str, object]]:
    if not candidates:
        return []
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for entry in candidates:
        grouped[str(entry["module"])].append(entry)
    job_specs: list[tuple[str, list[dict[str, object]], str]] = []
    for module_name, module_entries in sorted(grouped.items()):
        source_text = normalized_module_source_text(paths, module_name)
        line_starts = normalized_module_source_line_starts(paths, module_name)
        replay_end = max(
            offset_for_line_column(
                source_text,
                line_starts,
                int(entry["range"]["end_line"]),
                int(entry["range"]["end_column"]),
            )
            for entry in module_entries
        )
        job_specs.append((module_name, module_entries, source_text[:replay_end]))

    def run_probe_job(
        module_name: str,
        module_entries: list[dict[str, object]],
        prefix_source: str,
    ) -> tuple[str, list[dict[str, object]], Any, list[dict[str, object]]]:
        result, payloads = run_lean_source(
            paths,
            source=build_axiom_probe_source(
                prefix_source=prefix_source,
                file_entries=module_entries,
            ),
            description=f"按 axiom 语义探测模块 `{module_name}` 中的 permitted declarations",
            allow_failure=True,
        )
        return module_name, module_entries, result, payloads

    max_workers = min(len(job_specs), max(1, min(os.cpu_count() or 1, 4)))
    if max_workers <= 1:
        job_results = [
            run_probe_job(module_name, module_entries, prefix_source)
            for module_name, module_entries, prefix_source in job_specs
        ]
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(run_probe_job, module_name, module_entries, prefix_source)
                for module_name, module_entries, prefix_source in job_specs
            ]
            job_results = [future.result() for future in futures]

    result_by_module = {
        module_name: (module_entries, result, payloads)
        for module_name, module_entries, result, payloads in job_results
    }

    normalized: list[dict[str, object]] = []
    for module_name, _, _ in job_specs:
        module_entries, result, payloads = result_by_module[module_name]
        decl_names = [str(entry["decl_name"]) for entry in module_entries]
        payload_by_name = {str(payload["decl_name"]): payload for payload in payloads}
        missing_decl_names = [
            decl_name for decl_name in decl_names if decl_name not in payload_by_name
        ]
        if missing_decl_names:
            preview = missing_decl_names[:10]
            details = [
                f"模块：`{module_name}`",
                f"缺失数量：{len(missing_decl_names)}",
                "缺失声明：\n" + "\n".join(f"- {decl_name}" for decl_name in preview),
            ]
            if result.returncode != 0:
                output = "\n".join(
                    part for part in [result.stdout.strip(), result.stderr.strip()] if part
                )
                if output:
                    details.append("临时 probe 输出：\n" + output)
            if len(missing_decl_names) > len(preview):
                details.append(f"其余缺失条目：{len(missing_decl_names) - len(preview)} 个")
            fail(
                "axiom probe 没有返回全部候选声明。",
                details=details,
                hints=[
                    "这通常说明临时回放的源码前缀没有成功重建所需上下文。",
                    "先检查对应模块里是否存在非常规 theorem 头语法，或近期是否刚改过源码但未重建。",
                ],
            )
        for entry in module_entries:
            decl_name = str(entry["decl_name"])
            payload = payload_by_name[decl_name]
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
    relative_file = module_relative_file(paths, module_name)
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


def slice_command_text_for_decl(text: str, line_starts: list[int], range_info: dict[str, int]) -> str:
    start_line = int(range_info["line"])
    start_column = int(range_info["column"])
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


def decl_is_explicit_sorry_theorem(snippet: str) -> bool:
    if "theorem" not in snippet or "sorry" not in snippet:
        return False
    return decl_is_explicit_sorry_theorem_sanitized(strip_lean_comments_and_strings(snippet))


def decl_is_explicit_sorry_theorem_sanitized(sanitized_snippet: str) -> bool:
    if "theorem" not in sanitized_snippet or "sorry" not in sanitized_snippet:
        return False
    if decl_command_keyword_from_sanitized(sanitized_snippet) != "theorem":
        return False
    return SORRY_TOKEN_RE.search(sanitized_snippet) is not None


def module_may_contain_sorry_theorem(text: str) -> bool:
    if "sorry" not in text or "theorem" not in text:
        return False
    return module_may_contain_sorry_theorem_sanitized(strip_lean_comments_and_strings(text))


def module_may_contain_sorry_theorem_sanitized(sanitized_text: str) -> bool:
    if "sorry" not in sanitized_text or "theorem" not in sanitized_text:
        return False
    return SORRY_TOKEN_RE.search(sanitized_text) is not None and THEOREM_TOKEN_RE.search(sanitized_text) is not None


def collect_permitted_axioms(
    paths,
    target_info: dict[str, object],
    *,
    module_closure: list[str] | None = None,
) -> tuple[list[str], list[dict[str, object]]]:
    target_module = str(target_info["module"])
    closure = (
        list(module_closure)
        if module_closure is not None
        else compute_module_closure(paths, target_module)
    )
    target_decl = str(target_info["decl_name"])
    candidate_entries: list[dict[str, object]] = []
    for module_name in closure:
        source_text = normalized_module_source_text(paths, module_name)
        if "sorry" not in source_text or "theorem" not in source_text:
            continue
        sanitized_text = sanitized_module_source_text(paths, module_name)
        line_starts: list[int] | None = None
        sanitized_scan_text = sanitized_text
        if module_name == target_module:
            line_starts = normalized_module_source_line_starts(paths, module_name)
            target_scan_end = offset_for_line_column(
                source_text,
                line_starts,
                int(target_info["range"]["line"]),
                int(target_info["range"]["column"]),
            )
            sanitized_scan_text = sanitized_text[:target_scan_end]
        if not module_may_contain_sorry_theorem_sanitized(sanitized_scan_text):
            continue
        if line_starts is None:
            line_starts = normalized_module_source_line_starts(paths, module_name)
        entries = module_decl_entries_from_ilean(paths, module_name)
        if not entries:
            continue
        for entry in entries:
            if str(entry["decl_name"]) == target_decl:
                continue
            if module_name == target_module and not is_strictly_before(entry, target_info):
                break
            sanitized_snippet = slice_command_text_for_decl(sanitized_text, line_starts, entry["range"])
            if not decl_is_explicit_sorry_theorem_sanitized(sanitized_snippet):
                continue
            candidate_entries.append(entry)
    permitted_by_name: dict[str, dict[str, object]] = {}
    for decl_info in collect_axiomized_probed_declarations(paths, candidates=candidate_entries):
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


def resolve_mismatch_decl_name(
    module_entries: list[dict[str, object]],
    raw_decl_name: str,
) -> str | None:
    exact_matches = [
        str(entry["decl_name"]) for entry in module_entries if str(entry["decl_name"]) == raw_decl_name
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]
    short_matches = [
        str(entry["decl_name"])
        for entry in module_entries
        if short_decl_name(str(entry["decl_name"])) == raw_decl_name
    ]
    if len(short_matches) == 1:
        return short_matches[0]
    suffix_matches = [
        str(entry["decl_name"])
        for entry in module_entries
        if str(entry["decl_name"]).endswith("." + raw_decl_name)
    ]
    if len(suffix_matches) == 1:
        return suffix_matches[0]
    return None


def parse_temporary_axiom_hash_mismatches(
    output: str,
    *,
    module_entries: list[dict[str, object]],
) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for match in TEMP_AXIOM_HASH_MISMATCH_RE.finditer(output):
        raw_decl_name = match.group("decl").strip()
        full_decl_name = resolve_mismatch_decl_name(module_entries, raw_decl_name)
        if full_decl_name is None:
            continue
        resolved[full_decl_name] = match.group("actual")
    return resolved


def verify_prepared_axiom_hashes(
    paths,
    *,
    target_decl: str,
    target_module: str,
    module_closure: list[str],
    permitted_axioms: list[dict[str, object]],
) -> None:
    if not permitted_axioms:
        return
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for entry in permitted_axioms:
        grouped[str(entry["module"])].append(entry)
    module_order = [
        module_name
        for module_name in dependency_first_module_order(paths, module_closure)
        if module_name in grouped
    ]
    changed_any = False
    target_built_with_latest_runtime = False
    for module_name in module_order:
        module_entries = grouped[module_name]
        result = run_command(
            paths,
            ["lake", "build", module_name],
            f"校验 `{module_name}` 中 temporary axioms 的 statement hash",
            allow_failure=True,
        )
        if result.returncode == 0:
            if module_name == target_module:
                target_built_with_latest_runtime = True
            continue
        output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
        mismatches = parse_temporary_axiom_hash_mismatches(
            output,
            module_entries=module_entries,
        )
        if not mismatches:
            fail(
                "prepare 阶段校验 temporary axioms 时构建失败。",
                details=[
                    f"模块：`{module_name}`",
                    f"命令：`lake build {module_name}`",
                    "输出：\n" + (output or "<空>"),
                ],
                hints=[
                    "先检查该模块是否存在与 temporary axioms 无关的编译错误。",
                ],
            )
        module_changed = False
        for entry in module_entries:
            decl_name = str(entry["decl_name"])
            actual_hash = mismatches.get(decl_name)
            if actual_hash is None:
                continue
            if str(entry["statement_hash"]) != actual_hash:
                entry["statement_hash"] = actual_hash
                module_changed = True
        if not module_changed:
            fail(
                "prepare 阶段拿到了 hash mismatch，但没有产生新的 hash 更新。",
                details=[
                    f"模块：`{module_name}`",
                    "输出：\n" + (output or "<空>"),
                ],
                hints=[
                    "这通常说明 mismatch 解析结果和当前 generated runtime 已经不一致。",
                ],
            )
        write_generated_runtime(
            paths,
            target_decl=target_decl,
            permitted_axioms=permitted_axioms,
        )
        changed_any = True
        target_built_with_latest_runtime = False
    if not changed_any or target_built_with_latest_runtime:
        return
    result = run_command(
        paths,
        ["lake", "build", target_module],
        f"确认更新 hash 后 `{target_module}` 可以重新构建",
        allow_failure=True,
    )
    if result.returncode == 0:
        return
    output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
    if "The frozen prepared-session entry does not match the elaborated statement." in output:
        fail(
            "单轮 `verify` 更新 hash 后，target 模块里仍然存在 temporary axiom hash mismatch。",
            details=[
                f"目标模块：`{target_module}`",
                "输出：\n" + (output or "<空>"),
            ],
            hints=[
                "这说明至少有一个模块的一次构建没有暴露全部 mismatch，需要进一步分析该模块的报错行为。",
            ],
        )
    fail(
        "prepare 阶段在完成 hash 更新后，target 模块仍然构建失败。",
        details=[
            f"目标模块：`{target_module}`",
            "输出：\n" + (output or "<空>"),
        ],
        hints=[
            "先检查该失败是否与 temporary axioms 无关。",
        ],
    )


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
                decl_name = managed_attr_decl_name(line)
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


def attr_marker_line(decl_name: str) -> str:
    return f"@[temporary_axiom] {MANAGED_ATTR_PREFIX} {decl_name}"


def attr_marker_block_comment(decl_name: str) -> str:
    return f"/- {MANAGED_ATTR_PREFIX} {decl_name} -/"


def managed_attr_decl_name(line: str) -> str | None:
    if MANAGED_ATTR_PREFIX not in line:
        return None
    suffix = line.partition(f"{MANAGED_ATTR_PREFIX} ")[2].strip()
    if not suffix:
        return None
    return suffix.split()[0]


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


def normalize_managed_line(line: str) -> list[str]:
    if MANAGED_IMPORT_MARKER in line:
        return []
    if MANAGED_ATTR_PREFIX not in line:
        return [line]
    decl_name = managed_attr_decl_name(line)
    if decl_name is None:
        return [line]
    stripped = line.lstrip()
    if stripped.startswith("@[temporary_axiom]"):
        return []
    if stripped.startswith("temporary_axiom,"):
        return []
    cleaned = line.replace(attr_marker_block_comment(decl_name), "").rstrip()
    marker = f" {MANAGED_ATTR_PREFIX} {decl_name}"
    if marker in cleaned:
        cleaned = cleaned.replace(marker, "", 1).rstrip()
    cleaned = cleaned.replace("@[temporary_axiom, ", "@[", 1)
    cleaned = re.sub(
        r"\]\s+(?=(?:--|public\b|private\b|protected\b|noncomputable\b|unsafe\b|partial\b|local\b|scoped\b|theorem\b|axiom\b))",
        "] ",
        cleaned,
    )
    if cleaned.strip() == "@[":
        indent = cleaned[: len(cleaned) - len(cleaned.lstrip())]
        return [indent + "@["]
    return [cleaned.rstrip()]


def normalize_managed_lines(lines: list[str]) -> tuple[list[str], bool]:
    normalized: list[str] = []
    changed = False
    for line in lines:
        replacement = normalize_managed_line(line)
        if len(replacement) != 1 or replacement[0] != line:
            changed = True
        normalized.extend(replacement)
    return normalized, changed


def normalize_managed_text(text: str) -> tuple[str, bool]:
    normalized_lines, changed = normalize_managed_lines(text.splitlines())
    normalized_text = "\n".join(normalized_lines)
    if text.endswith("\n"):
        normalized_text += "\n"
    return normalized_text, changed


def decl_header_bounds(lines: list[str], entry: dict[str, object]) -> tuple[int, int, int]:
    range_info = entry["range"]
    assert isinstance(range_info, dict)
    start_idx = int(range_info["line"]) - 1
    selection_idx = int(range_info.get("selection_line", range_info["line"])) - 1
    if start_idx < 0 or selection_idx < 0 or selection_idx > len(lines):
        fail(
            "声明的源码范围超出文件边界。",
            details=[
                f"声明：`{entry['decl_name']}`",
                f"起始行：{range_info['line']}",
                f"选择行：{range_info.get('selection_line', range_info['line'])}",
            ],
            hints=[
                "这通常说明源码和 `.ilean` 不一致；请先重新构建相关模块。",
            ],
        )
    return start_idx, selection_idx, min(selection_idx + 1, len(lines))


def existing_attribute_index(lines: list[str], entry: dict[str, object]) -> int | None:
    start_idx, _, header_end = decl_header_bounds(lines, entry)
    for idx in range(max(0, start_idx), header_end):
        if lines[idx].lstrip().startswith("@["):
            return idx
    return None


def attribute_insertion_index(lines: list[str], entry: dict[str, object]) -> int:
    _, selection_idx, _ = decl_header_bounds(lines, entry)
    attr_idx = existing_attribute_index(lines, entry)
    if attr_idx is not None:
        return attr_idx
    return selection_idx


def find_attr_block_close_in_line(line: str) -> int | None:
    start = line.find("@[")
    if start < 0:
        return None
    depth = 0
    for idx in range(start, len(line)):
        char = line[idx]
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return idx
    return None


def attr_block_indent(lines: list[str], attr_idx: int, header_end: int) -> str:
    for idx in range(attr_idx + 1, header_end):
        stripped = lines[idx].lstrip()
        if stripped and stripped != "]":
            return lines[idx][: len(lines[idx]) - len(stripped)]
    return "  "


def patch_single_line_attr(line: str, decl_name: str) -> str:
    close_idx = find_attr_block_close_in_line(line)
    if close_idx is None:
        fail(
            "无法定位单行 attribute block 的结束位置。",
            details=[f"声明：`{decl_name}`", f"源码：`{line.strip()}`"],
        )
    marker = f" {attr_marker_block_comment(decl_name)}"
    patched = line.replace("@[", "@[temporary_axiom, ", 1)
    delta = len("@[temporary_axiom, ") - len("@[")
    insert_pos = close_idx + delta + 1
    return patched[:insert_pos] + marker + patched[insert_pos:]


def patch_existing_attribute_block(lines: list[str], entry: dict[str, object]) -> None:
    decl_name = str(entry["decl_name"])
    attr_idx = existing_attribute_index(lines, entry)
    if attr_idx is None:
        fail(
            "内部错误：预期存在 attribute block，但未找到起始行。",
            details=[f"声明：`{decl_name}`"],
        )
    _, _, header_end = decl_header_bounds(lines, entry)
    attr_line = lines[attr_idx]
    if find_attr_block_close_in_line(attr_line) is not None:
        lines[attr_idx] = patch_single_line_attr(attr_line, decl_name)
        return
    indent = attr_block_indent(lines, attr_idx, header_end)
    lines.insert(attr_idx + 1, f"{indent}temporary_axiom, {attr_marker_block_comment(decl_name)}")


def decl_header_contains_temporary_axiom(text: str, line_starts: list[int], range_info: dict[str, int]) -> bool:
    snippet = slice_command_text_for_decl(text, line_starts, range_info)
    if "temporary_axiom" not in snippet:
        return False
    sanitized = strip_lean_comments_and_strings(snippet)
    idx = 0
    while idx < len(sanitized):
        while idx < len(sanitized) and sanitized[idx].isspace():
            idx += 1
        if idx >= len(sanitized):
            return False
        if sanitized.startswith("@[", idx):
            bracket_depth = 1
            probe = idx + 2
            while probe < len(sanitized) and bracket_depth > 0:
                char = sanitized[probe]
                if char == "[":
                    bracket_depth += 1
                elif char == "]":
                    bracket_depth -= 1
                probe += 1
            if "temporary_axiom" in sanitized[idx:probe]:
                return True
            idx = probe
            continue
        match = LEAN_IDENT_RE.match(sanitized, idx)
        if match is None:
            return False
        token = match.group(0)
        idx = match.end()
        if token in DECL_MODIFIER_TOKENS:
            continue
        return False
    return False


def add_managed_attributes(
    *,
    text: str,
    lines: list[str],
    file_entries: list[dict[str, object]],
) -> list[dict[str, str]]:
    line_starts = build_line_starts(text)
    inserted_for: list[dict[str, str]] = []
    for entry in sorted(
        file_entries,
        key=lambda item: (
            attribute_insertion_index(lines, item),
            int(item["range"].get("selection_line", item["range"]["line"])),
            str(item["decl_name"]),
        ),
        reverse=True,
    ):
        decl_name = str(entry["decl_name"])
        range_info = entry["range"]
        assert isinstance(range_info, dict)
        if decl_header_contains_temporary_axiom(text, line_starts, range_info):
            continue
        if existing_attribute_index(lines, entry) is not None:
            patch_existing_attribute_block(lines, entry)
            inserted_for.append({"decl_name": decl_name, "mode": "patched_existing_attr"})
        else:
            lines.insert(attribute_insertion_index(lines, entry), attr_marker_line(decl_name))
            inserted_for.append({"decl_name": decl_name, "mode": "inserted_line"})
    inserted_for.reverse()
    return inserted_for


def write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def apply_prepare_edits(
    paths,
    permitted_axioms: list[dict[str, object]],
) -> dict[str, list[dict[str, str]]]:
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
            if text_contains_managed_markers(original_text):
                lines, _ = normalize_managed_lines(original_text.splitlines())
            else:
                lines = original_text.splitlines()
            normalized_text = "\n".join(lines) + "\n"
            inserted_decls = add_managed_attributes(
                text=normalized_text,
                lines=lines,
                file_entries=file_entries,
            )
            if file_entries and add_managed_import(lines):
                edits["imports"].append({"file": relative_file})
            for attr_edit in inserted_decls:
                edits["attributes"].append(
                    {
                        "decl_name": attr_edit["decl_name"],
                        "file": relative_file,
                        "mode": attr_edit["mode"],
                    }
                )
            if "\n".join(lines) + "\n" != original_text:
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
                decl_is_short_name=requested_target.decl_is_short_name,
            )
            fail_existing_session_prepare(
                paths,
                requested_target=str(requested_target_info["decl_name"]),
                requested_hash=str(requested_target_info["statement_hash"]),
                session_payload=session_payload,
            )
        if not inspection.runtime_is_reset:
            reset_generated_runtime(paths)

        candidate_modules: list[str] | None = None
        if requested_target.module_name is None:
            candidate_modules = candidate_target_modules_from_decl_name(paths, requested_target.decl_name)
            if not candidate_modules:
                fail(
                    "无法从完整声明名推断候选模块。",
                    details=[
                        f"目标声明：`{requested_target.decl_name}`",
                    ],
                    hints=[
                        "如果该声明名与模块路径不对齐，请改用 `--target <module>:<decl>`。",
                    ],
                )

        ready_candidate_modules: list[str] | None = None
        prepared_closures_by_root: dict[str, list[str]] = {}
        if requested_target.module_name is not None:
            print("Checking build artifacts for the target module closure...", flush=True)
            prepared_closures_by_root[requested_target.module_name] = ensure_module_artifacts_ready(
                paths,
                requested_target=args.target,
                root_module=requested_target.module_name,
                auto_build=args.auto_build,
            )
        else:
            assert candidate_modules is not None
            print("Checking build artifacts for candidate target modules...", flush=True)
            candidate_closures, issue_by_module = collect_artifact_issue_state_for_roots(
                paths,
                candidate_modules,
            )
            prepared_closures_by_root.update(candidate_closures)
            ready_candidate_modules = []
            if not args.auto_build:
                blocked_issues: list[ArtifactIssue] = []
                for candidate_module in candidate_modules:
                    issues = artifact_issues_for_closure(candidate_closures[candidate_module], issue_by_module)
                    if issues:
                        blocked_issues.extend(issues)
                        continue
                    ready_candidate_modules.append(candidate_module)
                if not ready_candidate_modules and blocked_issues:
                    fail_for_artifact_issues(
                        paths,
                        requested_target=args.target,
                        root_modules=candidate_modules,
                        issues=blocked_issues,
                    )
            else:
                remaining_issue_by_module = dict(issue_by_module)
                for candidate_module in candidate_modules:
                    issues = artifact_issues_for_closure(
                        candidate_closures[candidate_module],
                        remaining_issue_by_module,
                    )
                    if issues:
                        print(f"Refreshing artifacts for candidate module `{candidate_module}`...", flush=True)
                        print(format_artifact_issue_preview(dedupe_artifact_issues(issues)), flush=True)
                        ensure_module_artifacts(paths, candidate_module)
                        for module_name in candidate_closures[candidate_module]:
                            remaining_issue_by_module.pop(module_name, None)
                    ready_candidate_modules.append(candidate_module)
        ensure_probe_tool_ready(paths)
        target_info = resolve_target_decl(
            paths,
            decl_name=requested_target.decl_name,
            module_name=requested_target.module_name,
            decl_is_short_name=requested_target.decl_is_short_name,
            candidate_modules=ready_candidate_modules,
        )
        print("Scanning module closure for explicit `sorry` theorems...", flush=True)
        module_closure, permitted_axioms = collect_permitted_axioms(
            paths,
            target_info,
            module_closure=prepared_closures_by_root.get(str(target_info["module"])),
        )
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
            if args.verify and permitted_axioms:
                print("Verifying temporary axiom hashes in prepared modules...", flush=True)
            if args.verify:
                verify_prepared_axiom_hashes(
                    paths,
                    target_decl=str(target_info["decl_name"]),
                    target_module=str(target_info["module"]),
                    module_closure=module_closure,
                    permitted_axioms=permitted_axioms,
                )
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
            verified=args.verify,
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
            decl_name = managed_attr_decl_name(line)
            if decl_name is None:
                updated.append(line)
                continue
            mode = attr_modes.get(decl_name)
            if mode is None:
                updated.append(line)
                continue
            if mode == "inserted_line":
                continue
            updated.extend(normalize_managed_line(line))
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
        help="Target spec. Accepts either `<module>:<short-decl-or-fully-qualified-decl>` or a fully qualified declaration name.",
    )
    prepare.add_argument(
        "--base-commit",
        help="Optional base commit note. Defaults to the current HEAD when available.",
    )
    prepare.add_argument(
        "--auto-build",
        action="store_true",
        help="Automatically refresh stale or missing module artifacts before resolving the target.",
    )
    prepare.set_defaults(verify=True)
    prepare.add_argument(
        "--no-verify",
        dest="verify",
        action="store_false",
        help="Skip the prepare-time temporary-axiom hash verification pass and trust the frozen hashes from offline replay.",
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
