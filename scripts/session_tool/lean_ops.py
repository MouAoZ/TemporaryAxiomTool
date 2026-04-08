from __future__ import annotations

import json
import shlex
import subprocess
import tempfile
from pathlib import Path

from .common import (
    TOOL_NAMESPACE,
    TOOL_PREPARED_SESSION_MODULE,
    TOOL_PREPARED_SESSION_TYPES_MODULE,
    ensure_layout,
    fail,
    module_name_to_relative_path,
)


def run_command(
    paths,
    args: list[str],
    description: str,
    *,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            cwd=paths.project_root,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        fail(
            f"{description} 无法启动。",
            details=[
                f"命令：`{shlex.join(args)}`",
                f"缺失可执行文件：`{args[0]}`",
            ],
            hints=[
                "确认对应命令已经安装，并且在当前 shell 的 PATH 里。",
            ],
        )
    if result.returncode != 0 and not allow_failure:
        output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
        fail(
            f"{description} 失败。",
            details=[
                f"命令：`{shlex.join(args)}`",
                f"退出码：{result.returncode}",
                f"输出：\n{output}" if output else "输出：<空>",
            ],
        )
    return result


def try_git_head(paths) -> str | None:
    result = run_command(
        paths,
        ["git", "rev-parse", "HEAD"],
        "读取当前 HEAD",
        allow_failure=True,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def build_tool(paths) -> None:
    run_command(paths, ["lake", "build", paths.build_target], f"构建 `{paths.build_target}`")


def build_module(paths, module_name: str) -> None:
    run_command(paths, ["lake", "build", module_name], f"构建 `{module_name}`")


TEXT_HASH_RUNNER_SOURCE = """import Lake
open System

def main (args : List String) : IO UInt32 := do
  for arg in args do
    let hash <- Lake.computeTextFileHash (FilePath.mk arg)
    IO.println s!\"{arg}\\t{hash}\"
  return 0
"""


def module_artifact_path(paths, module_name: str, suffix: str) -> Path:
    return paths.lean_build_lib_root / module_name_to_relative_path(module_name).with_suffix(suffix)


def trace_artifact_path(paths, module_name: str) -> Path:
    return module_artifact_path(paths, module_name, ".trace")


def trace_source_hash(trace_path: Path, *, source_path: Path, relative_source_suffix: str) -> str | None:
    try:
        trace_data = json.loads(trace_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    inputs = trace_data.get("inputs", [])
    if not isinstance(inputs, list):
        return None
    resolved_source_path = source_path.resolve()
    for item in inputs:
        if not isinstance(item, list) or len(item) < 2:
            continue
        caption = str(item[0])
        value = item[1]
        if not isinstance(value, str):
            continue
        caption_matches = False
        try:
            caption_matches = Path(caption).resolve() == resolved_source_path
        except OSError:
            caption_matches = False
        if not caption_matches:
            caption_matches = caption.replace("\\", "/").endswith(relative_source_suffix)
        if not caption_matches:
            continue
        normalized = value.strip().lower()
        if len(normalized) == 16 and all(ch in "0123456789abcdef" for ch in normalized):
            return normalized
    return None


def compute_text_file_hashes(paths, file_paths: list[Path]) -> dict[Path, str]:
    resolved_paths: list[Path] = []
    seen: set[Path] = set()
    for path in file_paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        resolved_paths.append(resolved)
    if not resolved_paths:
        return {}
    with tempfile.NamedTemporaryFile(
        "w",
        suffix=".lean",
        prefix="TemporaryAxiomToolHash_",
        dir=paths.project_root,
        encoding="utf-8",
        delete=False,
    ) as handle:
        handle.write(TEXT_HASH_RUNNER_SOURCE)
        temp_path = Path(handle.name)
    try:
        result = run_command(
            paths,
            ["lake", "env", "lean", "--run", str(temp_path), *[str(path) for path in resolved_paths]],
            "计算源码文本哈希",
        )
    finally:
        temp_path.unlink(missing_ok=True)
    hashes: dict[Path, str] = {}
    for line in result.stdout.splitlines():
        raw_path, sep, raw_hash = line.partition("\t")
        if not sep:
            continue
        hashes[Path(raw_path).resolve()] = raw_hash.strip().lower()
    missing_paths = [str(path) for path in resolved_paths if path not in hashes]
    if missing_paths:
        preview = missing_paths[:10]
        details = [
            f"缺失数量：{len(missing_paths)}",
            "缺失文件：\n" + "\n".join(f"- `{path}`" for path in preview),
        ]
        if len(missing_paths) > len(preview):
            details.append(f"其余缺失条目：{len(missing_paths) - len(preview)} 个")
        fail(
            "Lean 文本哈希工具没有返回全部请求文件。",
            details=details,
        )
    return hashes


def compute_text_hashes_for_texts(paths, texts_by_path: dict[Path, str]) -> dict[Path, str]:
    resolved_items: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for path, text in texts_by_path.items():
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        resolved_items.append((resolved, text))
    if not resolved_items:
        return {}
    with tempfile.TemporaryDirectory(
        prefix="TemporaryAxiomToolNormalizedHash_",
        dir=paths.project_root,
    ) as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        temp_paths: list[Path] = []
        original_by_temp: dict[Path, Path] = {}
        for idx, (original_path, text) in enumerate(resolved_items):
            temp_path = temp_dir / f"normalized_{idx}.lean"
            temp_path.write_text(text, encoding="utf-8")
            temp_paths.append(temp_path)
            original_by_temp[temp_path.resolve()] = original_path
        raw_hashes = compute_text_file_hashes(paths, temp_paths)
    hashes: dict[Path, str] = {}
    for temp_path, hash_value in raw_hashes.items():
        original_path = original_by_temp.get(temp_path.resolve())
        if original_path is not None:
            hashes[original_path] = hash_value
    missing_paths = [str(path) for path, _ in resolved_items if path not in hashes]
    if missing_paths:
        preview = missing_paths[:10]
        details = [
            f"缺失数量：{len(missing_paths)}",
            "缺失文件：\n" + "\n".join(f"- `{path}`" for path in preview),
        ]
        if len(missing_paths) > len(preview):
            details.append(f"其余缺失条目：{len(missing_paths) - len(preview)} 个")
        fail(
            "Lean 文本哈希工具没有返回全部规范化源码。",
            details=details,
        )
    return hashes


def ensure_probe_tool_ready(paths) -> None:
    required_modules = [
        f"{TOOL_NAMESPACE}.StatementHash",
        f"{TOOL_NAMESPACE}.PreparedSession.Types",
        f"{TOOL_NAMESPACE}.PreparedSession",
    ]
    required_sources = [
        paths.project_root / TOOL_NAMESPACE / "StatementHash.lean",
        paths.project_root / TOOL_NAMESPACE / "PreparedSession" / "Types.lean",
        paths.project_root / TOOL_NAMESPACE / "PreparedSession.lean",
    ]
    for module_name in required_modules:
        olean_path = module_artifact_path(paths, module_name, ".olean")
        ilean_path = module_artifact_path(paths, module_name, ".ilean")
        trace_path = trace_artifact_path(paths, module_name)
        if not olean_path.exists() or not ilean_path.exists() or not trace_path.exists():
            build_tool(paths)
            return
    current_hashes = compute_text_file_hashes(paths, required_sources)
    for module_name, source_path in zip(required_modules, required_sources, strict=True):
        trace_path = trace_artifact_path(paths, module_name)
        recorded_hash = trace_source_hash(
            trace_path,
            source_path=source_path,
            relative_source_suffix=source_path.relative_to(paths.project_root).as_posix(),
        )
        if recorded_hash is None:
            build_tool(paths)
            return
        current_hash = current_hashes.get(source_path.resolve())
        if current_hash != recorded_hash:
            build_tool(paths)
            return


def run_lean_source(
    paths,
    *,
    source: str,
    description: str,
    allow_failure: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]]]:
    with tempfile.NamedTemporaryFile(
        "w",
        suffix=".lean",
        prefix="TemporaryAxiomToolProbe_",
        dir=paths.project_root,
        encoding="utf-8",
        delete=False,
    ) as handle:
        handle.write(source)
        temp_path = Path(handle.name)
    try:
        result = run_command(
            paths,
            ["lake", "env", "lean", str(temp_path)],
            description,
            allow_failure=allow_failure,
        )
    finally:
        temp_path.unlink(missing_ok=True)
    payloads: list[dict[str, object]] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            payloads.append(json.loads(stripped))
    return result, payloads


def run_lean_probe(
    paths,
    *,
    imports: list[str],
    command_lines: list[str],
    description: str,
    allow_failure: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]]]:
    lines = ["module", f"import {TOOL_PREPARED_SESSION_MODULE}"]
    for module_name in imports:
        lines.append(f"import {module_name}")
    lines.append("")
    lines.extend(command_lines)
    source = "\n".join(lines) + "\n"
    return run_lean_source(
        paths,
        source=source,
        description=description,
        allow_failure=allow_failure,
    )


def lean_name_literal(name: str) -> str:
    return f"`{name}"


def try_probe_decl_in_module(paths, module_name: str, decl_name: str) -> dict[str, object] | None:
    result, payloads = run_lean_probe(
        paths,
        imports=[module_name],
        command_lines=[f"#print_temporary_axiom_decl_probe {lean_name_literal(decl_name)}"],
        description=f"探测声明 `{decl_name}`",
        allow_failure=True,
    )
    if result.returncode != 0 or not payloads:
        return None
    return payloads[0]


def probe_named_declarations_with_imports(
    paths,
    *,
    imports: list[str],
    decl_names: list[str],
    description: str,
) -> list[dict[str, object]]:
    if not decl_names:
        return []
    payloads: list[dict[str, object]] = []
    chunk_size = 200
    for start in range(0, len(decl_names), chunk_size):
        chunk = decl_names[start:start + chunk_size]
        _, chunk_payloads = run_lean_probe(
            paths,
            imports=imports,
            command_lines=[
                f"#print_temporary_axiom_decl_probe {lean_name_literal(decl_name)}"
                for decl_name in chunk
            ],
            description=description,
            allow_failure=False,
        )
        payloads.extend(chunk_payloads)
    return payloads


def generated_runtime_source(*, target_decl: str | None, permitted_axioms: list[dict[str, object]]) -> str:
    entry_lines: list[str] = []
    for entry in permitted_axioms:
        entry_lines.append(
            "  {\n"
            f"    name := `{entry['decl_name']}\n"
            f"    statementHash := ({entry['statement_hash']} : UInt64)\n"
            "  }"
        )
    entries_block = ",\n".join(entry_lines)
    target_name_expr = "Lean.Name.anonymous" if target_decl is None else f"`{target_decl}"
    permitted_array = "#[]" if not entry_lines else f"#[\n{entries_block}\n]"
    return (
        "module\n\n"
        "/-\n"
        "Auto-generated prepared-session runtime.\n"
        "This file is managed by TemporaryAxiomTool.\n"
        "-/\n"
        f"public import {TOOL_PREPARED_SESSION_TYPES_MODULE}\n\n"
        "namespace TemporaryAxiomTool.PreparedSession\n\n"
        f"public def generatedTargetName : Lean.Name := {target_name_expr}\n\n"
        f"public def generatedPermittedAxioms : Array PermittedAxiom := {permitted_array}\n\n"
        "end TemporaryAxiomTool.PreparedSession\n"
    )


def write_generated_runtime(paths, *, target_decl: str, permitted_axioms: list[dict[str, object]]) -> None:
    ensure_layout(paths)
    source = generated_runtime_source(
        target_decl=target_decl,
        permitted_axioms=permitted_axioms,
    )
    paths.generated_session_file.write_text(source, encoding="utf-8")


def reset_generated_runtime(paths) -> None:
    ensure_layout(paths)
    source = generated_runtime_source(
        target_decl=None,
        permitted_axioms=[],
    )
    paths.generated_session_file.write_text(source, encoding="utf-8")
