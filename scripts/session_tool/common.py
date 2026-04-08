from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


TOOL_NAMESPACE = "TemporaryAxiomTool"
TOOL_TEMPORARY_AXIOM_MODULE = f"{TOOL_NAMESPACE}.TemporaryAxiom"
TOOL_PREPARED_SESSION_MODULE = f"{TOOL_NAMESPACE}.PreparedSession"
TOOL_PREPARED_SESSION_TYPES_MODULE = f"{TOOL_PREPARED_SESSION_MODULE}.Types"
TOOL_PREPARED_SESSION_TARGET_MODULE = f"{TOOL_PREPARED_SESSION_MODULE}.Target"
TOOL_PREPARED_SESSION_PERMITTED_MODULE_PREFIX = f"{TOOL_PREPARED_SESSION_MODULE}.Permitted"
DEFAULT_SESSION_DIRNAME = ".temporary_axiom_session"
DEFAULT_REPORT_FILENAME = "temporary_axiom_tool_session_report.txt"
MANAGED_IMPORT_MARKER = "-- temporary_axiom_tool managed import"
MANAGED_ATTR_PREFIX = "-- temporary_axiom_tool managed attr:"
LEAN_FILE_SUFFIX = ".lean"
IMPORT_RE = re.compile(r"^\s*(?:public\s+)?import\s+(?P<mods>.+?)\s*$")


@dataclass(frozen=True)
class SessionPaths:
    project_root: Path
    session_root: Path
    session_file: Path
    prepare_lock_file: Path
    report_file: Path
    lean_prepared_session_root: Path
    generated_target_file: Path
    generated_permitted_root: Path
    legacy_generated_session_file: Path
    lean_build_lib_root: Path
    build_target: str


def make_paths(project_root: Path) -> SessionPaths:
    session_root = project_root / DEFAULT_SESSION_DIRNAME
    lean_prepared_session_root = project_root / TOOL_NAMESPACE / "PreparedSession"
    return SessionPaths(
        project_root=project_root,
        session_root=session_root,
        session_file=session_root / "session.json",
        prepare_lock_file=session_root / "prepare.lock",
        report_file=project_root / DEFAULT_REPORT_FILENAME,
        lean_prepared_session_root=lean_prepared_session_root,
        generated_target_file=lean_prepared_session_root / "Target.lean",
        generated_permitted_root=lean_prepared_session_root / "Permitted",
        legacy_generated_session_file=lean_prepared_session_root / "Generated.lean",
        lean_build_lib_root=project_root / ".lake" / "build" / "lib" / "lean",
        build_target=TOOL_NAMESPACE,
    )


def ensure_layout(paths: SessionPaths) -> None:
    paths.session_root.mkdir(parents=True, exist_ok=True)
    paths.lean_prepared_session_root.mkdir(parents=True, exist_ok=True)
    paths.generated_permitted_root.mkdir(parents=True, exist_ok=True)


def acquire_prepare_lock(paths: SessionPaths) -> None:
    ensure_layout(paths)
    try:
        fd = os.open(paths.prepare_lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        fail(
            "另一个 `prepare` 正在运行。",
            details=[
                f"锁文件：`{paths.prepare_lock_file}`",
            ],
            hints=[
                "等待当前 `prepare` 完成后再重试。",
                "如果上一次 `prepare` 异常退出，请先确认没有活跃进程后再删除这个锁文件。",
            ],
        )
    try:
        os.write(fd, str(os.getpid()).encode("utf-8"))
    finally:
        os.close(fd)


def release_prepare_lock(paths: SessionPaths) -> None:
    paths.prepare_lock_file.unlink(missing_ok=True)


def format_user_error(
    summary: str,
    *,
    details: Iterable[str] = (),
    hints: Iterable[str] = (),
) -> str:
    lines = [f"TemporaryAxiomTool 错误：{summary}"]
    details_list = [detail for detail in details if detail]
    hints_list = [hint for hint in hints if hint]
    if details_list:
        lines.append("")
        lines.append("详情：")
        lines.extend(f"- {detail}" for detail in details_list)
    if hints_list:
        lines.append("")
        lines.append("建议：")
        lines.extend(f"- {hint}" for hint in hints_list)
    return "\n".join(lines)


def fail(
    summary: str,
    *,
    details: Iterable[str] = (),
    hints: Iterable[str] = (),
) -> None:
    raise SystemExit(format_user_error(summary, details=details, hints=hints))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=False, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        fail(
            "读取 JSON 文件失败。",
            details=[
                f"文件：`{path}`",
                f"系统错误：{exc}",
            ],
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        fail(
            "JSON 文件格式无效。",
            details=[
                f"文件：`{path}`",
                f"位置：第 {exc.lineno} 行，第 {exc.colno} 列",
                f"解析器信息：{exc.msg}",
            ],
        )

def module_name_to_relative_path(module_name: str) -> Path:
    parts = module_name.split(".")
    return Path(*parts).with_suffix(LEAN_FILE_SUFFIX)


def module_name_to_path(project_root: Path, module_name: str) -> Path:
    return project_root / module_name_to_relative_path(module_name)


def is_host_project_module(project_root: Path, module_name: str) -> bool:
    return module_name_to_path(project_root, module_name).exists()
