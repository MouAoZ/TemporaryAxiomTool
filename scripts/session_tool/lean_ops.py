from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from .common import (
    TOOL_NAMESPACE,
    TOOL_ROOT_MODULE,
    TOOL_THEOREM_REGISTRY_MODULE,
    TOOL_THEOREM_REGISTRY_SHARDS_MODULE_PREFIX,
    TOOL_THEOREM_REGISTRY_TYPES_MODULE,
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
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            cwd=paths.project_root,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
    except FileNotFoundError:
        fail(
            f"{description} 无法启动。",
            details=[
                f"命令：`{shlex.join(args)}`",
                f"缺失可执行文件：`{args[0]}`",
            ],
            hints=["确认对应命令已经安装，并且在当前 shell 的 PATH 里。"],
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


@lru_cache(maxsize=None)
def lean_command_configuration(paths) -> tuple[tuple[str, ...], dict[str, str] | None]:
    lean_executable = shutil.which("lean")
    if lean_executable is None:
        return ("lake", "env", "lean"), None
    result = run_command(
        paths,
        ["lake", "env", "printenv", "LEAN_PATH"],
        "读取 LEAN_PATH",
        allow_failure=True,
    )
    lean_path = result.stdout.strip() if result.returncode == 0 else ""
    if not lean_path:
        return ("lake", "env", "lean"), None
    env = dict(os.environ)
    env["LEAN_PATH"] = lean_path
    return (lean_executable,), env


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


def compute_text_hashes_for_resolved_paths(paths, resolved_paths: list[Path]) -> dict[Path, str]:
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
    lean_cmd, lean_env = lean_command_configuration(paths)
    try:
        result = run_command(
            paths,
            [*lean_cmd, "--run", str(temp_path), *[str(path) for path in resolved_paths]],
            "计算源码文本哈希",
            env=lean_env,
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
        fail("Lean 文本哈希工具没有返回全部请求文件。", details=details)
    return hashes


def compute_text_hashes(
    paths,
    *,
    file_paths: list[Path] | None = None,
    texts_by_path: dict[Path, str] | None = None,
) -> dict[Path, str]:
    resolved_paths: list[Path] = []
    resolved_text_items: list[tuple[Path, str]] = []
    seen_raw: set[Path] = set()
    seen_text: set[Path] = set()
    for path in file_paths or []:
        resolved = path.resolve()
        if resolved in seen_raw:
            continue
        seen_raw.add(resolved)
        resolved_paths.append(resolved)
    for path, text in (texts_by_path or {}).items():
        resolved = path.resolve()
        if resolved in seen_text:
            continue
        seen_text.add(resolved)
        resolved_text_items.append((resolved, text))
    if not resolved_paths and not resolved_text_items:
        return {}
    with tempfile.TemporaryDirectory(
        prefix="TemporaryAxiomToolNormalizedHash_",
        dir=paths.project_root,
    ) as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        original_by_temp: dict[Path, Path] = {}
        hash_inputs = list(resolved_paths)
        for idx, (original_path, text) in enumerate(resolved_text_items):
            temp_path = temp_dir / f"normalized_{idx}.lean"
            temp_path.write_text(text, encoding="utf-8")
            temp_resolved = temp_path.resolve()
            original_by_temp[temp_resolved] = original_path
            hash_inputs.append(temp_resolved)
        raw_hashes = compute_text_hashes_for_resolved_paths(paths, hash_inputs)
    hashes: dict[Path, str] = {}
    for path, hash_value in raw_hashes.items():
        original_path = original_by_temp.get(path)
        if original_path is not None:
            hashes[original_path] = hash_value
        else:
            hashes[path] = hash_value
    return hashes


def compute_text_file_hashes(paths, file_paths: list[Path]) -> dict[Path, str]:
    return compute_text_hashes(paths, file_paths=file_paths)


def compute_text_hashes_for_texts(paths, texts_by_path: dict[Path, str]) -> dict[Path, str]:
    return compute_text_hashes(paths, texts_by_path=texts_by_path)


def ensure_probe_tool_ready(paths) -> None:
    required_modules = [
        f"{TOOL_NAMESPACE}.StatementHash",
        TOOL_THEOREM_REGISTRY_TYPES_MODULE,
        TOOL_THEOREM_REGISTRY_MODULE,
        f"{TOOL_NAMESPACE}.TemporaryAxiom",
    ]
    required_sources = [
        paths.project_root / TOOL_NAMESPACE / "StatementHash.lean",
        paths.project_root / TOOL_NAMESPACE / "TheoremRegistry" / "Types.lean",
        paths.project_root / TOOL_NAMESPACE / "TheoremRegistry.lean",
        paths.project_root / TOOL_NAMESPACE / "TemporaryAxiom.lean",
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
    extra_args: list[str] | None = None,
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
    lean_cmd, lean_env = lean_command_configuration(paths)
    try:
        result = run_command(
            paths,
            [*lean_cmd, *(extra_args or []), str(temp_path)],
            description,
            allow_failure=allow_failure,
            env=lean_env,
        )
    finally:
        temp_path.unlink(missing_ok=True)
    payloads: list[dict[str, object]] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                payloads.append(payload)
    return result, payloads


def run_lean_module_source(
    paths,
    *,
    module_name: str,
    source: str,
    description: str,
    allow_failure: bool = False,
    extra_args: list[str] | None = None,
    extra_sources: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]]]:
    with tempfile.TemporaryDirectory(
        prefix="TemporaryAxiomToolReplay_",
        dir=paths.project_root,
    ) as temp_root_name:
        temp_root = Path(temp_root_name)
        temp_path = temp_root / module_name_to_relative_path(module_name)
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(source, encoding="utf-8")
        for extra_module_name, extra_source in (extra_sources or {}).items():
            extra_path = temp_root / module_name_to_relative_path(extra_module_name)
            extra_path.parent.mkdir(parents=True, exist_ok=True)
            extra_path.write_text(extra_source, encoding="utf-8")
        lean_cmd, lean_env = lean_command_configuration(paths)
        effective_env = None if lean_env is None else dict(lean_env)
        if extra_sources:
            if effective_env is None:
                effective_env = dict(os.environ)
            existing_lean_path = effective_env.get("LEAN_PATH", "")
            effective_env["LEAN_PATH"] = (
                f"{temp_root}:{existing_lean_path}" if existing_lean_path else str(temp_root)
            )
        result = run_command(
            paths,
            [*lean_cmd, *(extra_args or []), f"--root={temp_root}", str(temp_path)],
            description,
            allow_failure=allow_failure,
            env=effective_env,
        )
    payloads: list[dict[str, object]] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                payloads.append(payload)
    return result, payloads


def run_lean_module_file(
    paths,
    *,
    module_path: Path,
    description: str,
    allow_failure: bool = False,
    extra_args: list[str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]]]:
    lean_cmd, lean_env = lean_command_configuration(paths)
    result = run_command(
        paths,
        [*lean_cmd, *(extra_args or []), str(module_path)],
        description,
        allow_failure=allow_failure,
        env=lean_env,
    )
    payloads: list[dict[str, object]] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                payloads.append(payload)
    return result, payloads


def compile_importable_lean_module(
    paths,
    *,
    source_path: Path,
    module_name: str,
    description: str,
) -> tuple[Path, Path]:
    lean_cmd, lean_env = lean_command_configuration(paths)
    olean_path = module_artifact_path(paths, module_name, ".olean")
    ilean_path = module_artifact_path(paths, module_name, ".ilean")
    olean_path.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        paths,
        [*lean_cmd, str(source_path), "-o", str(olean_path), "-i", str(ilean_path)],
        description,
        env=lean_env,
    )
    return olean_path, ilean_path


def run_lean_probe(
    paths,
    *,
    imports: list[str],
    command_lines: list[str],
    description: str,
    allow_failure: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]]]:
    lines = ["module", f"import {TOOL_THEOREM_REGISTRY_MODULE}"]
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

def lean_string_literal(text: str) -> str:
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def try_probe_decl_in_module(paths, module_name: str, decl_name: str) -> dict[str, object] | None:
    result, payloads = run_lean_probe(
        paths,
        imports=[module_name],
        command_lines=[f"#print_temporary_axiom_decl_probe_text {lean_string_literal(decl_name)}"],
        description=f"探测声明 `{decl_name}`",
        allow_failure=True,
    )
    if result.returncode != 0 or not payloads:
        return None
    return payloads[0]


def generated_shard_runtime_module_name(module_name: str) -> str:
    return f"{TOOL_THEOREM_REGISTRY_SHARDS_MODULE_PREFIX}.{module_name}"


def generated_shard_runtime_path(paths, module_name: str) -> Path:
    return paths.project_root / module_name_to_relative_path(generated_shard_runtime_module_name(module_name))


def generated_shard_module_source(
    *,
    module_name: str,
    mode: str,
    target_decl: str | None,
    target_hash: str | None,
    permitted_axioms: list[dict[str, object]],
) -> str:
    permitted_payload = "\n".join(
        f"{entry['decl_name']}\t{entry['statement_hash']}"
        for entry in sorted(permitted_axioms, key=lambda item: str(item["decl_name"]))
    )
    return (
        "module\n\n"
        "/-\n"
        "Auto-generated theorem-registry shard.\n"
        "This file is managed by TemporaryAxiomTool.\n"
        "-/\n"
        f"import {TOOL_THEOREM_REGISTRY_MODULE}\n\n"
        f"#register_temporary_axiom_module_shard "
        f"{lean_string_literal(module_name)} "
        f"{lean_string_literal(mode)} "
        f"{lean_string_literal(target_decl or '')} "
        f"{lean_string_literal(target_hash or '0')} "
        f"{lean_string_literal(permitted_payload)}\n"
    )


def generated_shard_sources(
    paths,
    *,
    tracked_modules: list[str],
    mode_by_module: dict[str, str],
    target_decl: str | None,
    target_hash: str | None,
    permitted_axioms: list[dict[str, object]],
) -> dict[Path, str]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for entry in permitted_axioms:
        grouped.setdefault(str(entry["module"]), []).append(entry)
    return {
        generated_shard_runtime_path(paths, module_name): generated_shard_module_source(
            module_name=module_name,
            mode=mode_by_module.get(module_name, "inactive"),
            target_decl=target_decl if mode_by_module.get(module_name, "inactive") == "active" else None,
            target_hash=target_hash if mode_by_module.get(module_name, "inactive") == "active" else None,
            permitted_axioms=grouped.get(module_name, []) if mode_by_module.get(module_name, "inactive") == "active" else [],
        )
        for module_name in tracked_modules
    }


def write_generated_shards(
    paths,
    *,
    tracked_modules: list[str],
    mode_by_module: dict[str, str],
    target_decl: str | None,
    target_hash: str | None,
    permitted_axioms: list[dict[str, object]],
) -> None:
    ensure_layout(paths)
    sources = generated_shard_sources(
        paths,
        tracked_modules=tracked_modules,
        mode_by_module=mode_by_module,
        target_decl=target_decl,
        target_hash=target_hash,
        permitted_axioms=permitted_axioms,
    )
    for path, source in sources.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_text(encoding="utf-8") == source:
            continue
        path.write_text(source, encoding="utf-8")
    expected_paths = set(sources)
    for path in sorted(paths.generated_shards_root.rglob("*.lean")):
        if path not in expected_paths:
            path.unlink(missing_ok=True)
    for directory in sorted(paths.generated_shards_root.rglob("*"), reverse=True):
        if directory.is_dir():
            try:
                directory.rmdir()
            except OSError:
                pass


def write_inactive_shards(
    paths,
    *,
    tracked_modules: list[str],
) -> None:
    write_generated_shards(
        paths,
        tracked_modules=tracked_modules,
        mode_by_module={module_name: "inactive" for module_name in tracked_modules},
        target_decl=None,
        target_hash=None,
        permitted_axioms=[],
    )


def write_collect_shards(
    paths,
    *,
    tracked_modules: list[str],
    collect_modules: list[str],
) -> None:
    collect_module_set = {str(module_name) for module_name in collect_modules}
    write_generated_shards(
        paths,
        tracked_modules=tracked_modules,
        mode_by_module={
            module_name: ("collect" if module_name in collect_module_set else "inactive")
            for module_name in tracked_modules
        },
        target_decl=None,
        target_hash=None,
        permitted_axioms=[],
    )


def write_active_shards(
    paths,
    *,
    tracked_modules: list[str],
    target_decl: str,
    target_hash: str,
    permitted_axioms: list[dict[str, object]],
) -> None:
    write_generated_shards(
        paths,
        tracked_modules=tracked_modules,
        mode_by_module={module_name: "active" for module_name in tracked_modules},
        target_decl=target_decl,
        target_hash=target_hash,
        permitted_axioms=permitted_axioms,
    )
