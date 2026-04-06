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


def module_artifact_path(paths, module_name: str, suffix: str) -> Path:
    return paths.lean_build_lib_root / module_name_to_relative_path(module_name).with_suffix(suffix)


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
    for module_name, source_path in zip(required_modules, required_sources, strict=True):
        olean_path = module_artifact_path(paths, module_name, ".olean")
        ilean_path = module_artifact_path(paths, module_name, ".ilean")
        if not olean_path.exists() or not ilean_path.exists():
            build_tool(paths)
            return
        source_mtime = source_path.stat().st_mtime_ns
        artifact_mtime = min(olean_path.stat().st_mtime_ns, ilean_path.stat().st_mtime_ns)
        if source_mtime > artifact_mtime:
            build_tool(paths)
            return


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
