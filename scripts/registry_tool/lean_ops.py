from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from .common import (
    TOOL_REGISTRY_MODULE,
    TOOL_REGISTRY_TYPES_MODULE,
    ensure_layout,
    lean_shard_const,
    lean_shard_module,
    lean_shard_stem,
)
from .db import load_current_shards


def run_command(paths, args: list[str], description: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            cwd=paths.project_root,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SystemExit(f"{description} 无法启动：找不到命令 `{args[0]}`。") from exc
    if result.returncode != 0:
        output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
        raise SystemExit(f"{description} 失败：\n{output}")
    return result


def probe_declarations(paths, module_name: str, decl_names: list[str]) -> list[dict[str, str]]:
    if not decl_names:
        return []
    lines = [
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
        result = run_command(paths, ["lake", "env", "lean", str(temp_path)], f"探测模块 `{module_name}`")
    finally:
        temp_path.unlink(missing_ok=True)
    payloads: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            payloads.append(json.loads(stripped))
    if len(payloads) != len(decl_names):
        raise SystemExit(
            f"探测模块 `{module_name}` 返回结果不完整：期望 {len(decl_names)} 条，实际解析到 {len(payloads)} 条。"
        )
    return payloads


def generate_lean_registry(paths) -> None:
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
    if consts:
        body = " ++\n  ".join(consts)
        import_block = "\n".join([f"import {TOOL_REGISTRY_TYPES_MODULE}", *imports])
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


def build_registry(paths) -> None:
    run_command(paths, ["lake", "build", paths.build_target], f"构建 `{paths.build_target}`")


def run_temporary_axiom_audit(paths, modules: list[str]) -> None:
    if not modules:
        raise SystemExit("Temporary axiom audit requires at least one --module argument.")
    with tempfile.NamedTemporaryFile(
        "w",
        suffix=".lean",
        prefix=".temporary_axiom_audit.generated.",
        dir=paths.project_root,
        encoding="utf-8",
        delete=False,
    ) as handle:
        generated_path = Path(handle.name)
        handle.write("/- Auto-generated by manage_approved_statement_registry.py. -/\n")
        handle.write("import TemporaryAxiomTool.TemporaryAxiom\n")
        for module_name in modules:
            handle.write(f"import {module_name}\n")
        handle.write("\n#assert_no_temporary_axioms\n")
    try:
        run_command(paths, ["lake", "env", "lean", str(generated_path)], "Temporary axiom audit")
        print("Temporary axiom audit passed.")
        print("Loaded modules:")
        for module_name in modules:
            print(f"- {module_name}")
    finally:
        generated_path.unlink(missing_ok=True)
