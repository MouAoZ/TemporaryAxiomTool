#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


README_BLOCK_START = "<!-- temporary-axiom-docs:start -->"
README_BLOCK_END = "<!-- temporary-axiom-docs:end -->"
TEMP_ATTR_RE = re.compile(r"^\s*@\[[^\]]*\btemporary_axiom\b[^\]]*\]")
THEOREM_NAME_RE = re.compile(r"\btheorem\s+([^\s:(]+)")
DEFAULT_TARGETS_RE = re.compile(r'^(\s*defaultTargets\s*=\s*)(.*)$')
LAKEFILE_NAME_RE = re.compile(r'^\s*name\s*=\s*"([^"]+)"\s*$')
WORKFLOW_BLOCK_MARKERS = (
    ("# temporary-axiom-audit:start", "# temporary-axiom-audit:end"),
    ("# approved-statement-registry-audit:start", "# approved-statement-registry-audit:end"),
)
WORKFLOW_AUDIT_SCRIPT_HINTS = {
    "run_temporary_axiom_audit.sh": (
        "# temporary-axiom-audit:start",
        "# temporary-axiom-audit:end",
    ),
    "run_approved_statement_registry_audit.sh": (
        "# approved-statement-registry-audit:start",
        "# approved-statement-registry-audit:end",
    ),
}


class CleanupFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class MarkerHit:
    path: Path
    line: int
    theorem_name: str | None


@dataclass(frozen=True)
class FileEdit:
    path: Path
    new_text: str


@dataclass(frozen=True)
class WorkflowReferenceHit:
    path: Path
    line: int
    script_name: str


@dataclass(frozen=True)
class CleanupConfig:
    project_root: Path
    utility_module: str
    utility_root_name: str
    utility_root_file: Path
    utility_dir: Path
    registry_db_dir: Path
    audit_script: Path
    audit_modules: tuple[str, ...]
    registry_audit_script: Path
    registry_tool_script: Path
    workflow_files: tuple[Path, ...]
    readme_file: Path
    lakefile: Path
    docs_files: tuple[Path, ...]
    execute: bool
    keep_docs: bool
    skip_audit: bool
    skip_build: bool


def parse_args() -> CleanupConfig:
    parser = argparse.ArgumentParser(
        description=(
            "Locate remaining temporary_axiom declarations and, once none remain, "
            "clean the temporary-axiom scaffolding from the project."
        )
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Path to the Lean project root. Defaults to the current directory.",
    )
    parser.add_argument(
        "--utility-module",
        default="TemporaryAxiomTool.TemporaryAxiom",
        help="Fully qualified module name for the temporary axiom utility.",
    )
    parser.add_argument(
        "--audit-module",
        action="append",
        default=[],
        help=(
            "Lean module imported into the generated temporary-axiom audit entry. "
            "Repeat to cover multiple root or section modules."
        ),
    )
    parser.add_argument(
        "--audit-script",
        default="scripts/run_temporary_axiom_audit.sh",
        help="Shell script used by CI/CD to run the generated temporary-axiom audit.",
    )
    parser.add_argument(
        "--workflow-file",
        action="append",
        default=[],
        help=(
            "Workflow file containing removable CI audit blocks delimited by "
            "the supported audit markers. Can be repeated. Defaults to every "
            "YAML file under .github/workflows/."
        ),
    )
    parser.add_argument(
        "--readme-file",
        default="README.md",
        help="README file containing the removable temporary-axiom docs block.",
    )
    parser.add_argument(
        "--lakefile",
        default="lakefile.toml",
        help="Lake configuration file that may contain the TemporaryAxiomTool lean_lib block.",
    )
    parser.add_argument(
        "--keep-docs",
        action="store_true",
        help="Keep the temporary-axiom documentation files instead of deleting them.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply cleanup changes. Without this flag, the script only prints the plan.",
    )
    parser.add_argument(
        "--skip-audit",
        action="store_true",
        help="Skip the pre-cleanup generated #assert_no_temporary_axioms check.",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Skip the post-cleanup lake build verification step.",
    )
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    utility_root_name = args.utility_module.split(".")[0]
    workflow_files = discover_workflow_files(project_root, args.workflow_file)

    return CleanupConfig(
        project_root=project_root,
        utility_module=args.utility_module,
        utility_root_name=utility_root_name,
        utility_root_file=project_root / f"{utility_root_name}.lean",
        utility_dir=project_root / utility_root_name,
        registry_db_dir=project_root / "approved_statement_registry_db",
        audit_script=project_root / args.audit_script,
        audit_modules=tuple(args.audit_module),
        registry_audit_script=project_root / "scripts/run_approved_statement_registry_audit.sh",
        registry_tool_script=project_root / "scripts/manage_approved_statement_registry.py",
        workflow_files=workflow_files,
        readme_file=project_root / args.readme_file,
        lakefile=project_root / args.lakefile,
        docs_files=(project_root / "docs/temporary_axiom.md",),
        execute=args.execute,
        keep_docs=args.keep_docs,
        skip_audit=args.skip_audit,
        skip_build=args.skip_build,
    )


def discover_workflow_files(project_root: Path, explicit_paths: list[str]) -> tuple[Path, ...]:
    if explicit_paths:
        return tuple(project_root / path for path in explicit_paths)
    workflow_dir = project_root / ".github" / "workflows"
    if not workflow_dir.exists():
        return ()
    discovered = sorted(
        list(workflow_dir.glob("*.yml")) + list(workflow_dir.glob("*.yaml"))
    )
    return tuple(discovered)


def iter_lean_files(project_root: Path) -> list[Path]:
    excluded_roots = {".git", ".lake"}
    results: list[Path] = []
    for path in project_root.rglob("*.lean"):
        rel_parts = path.relative_to(project_root).parts
        if rel_parts and rel_parts[0] in excluded_roots:
            continue
        if path.name.startswith(".temporary_axiom_audit.generated."):
            continue
        results.append(path)
    return sorted(results)


def infer_theorem_name(lines: list[str], start_idx: int) -> str | None:
    for idx in range(start_idx, min(len(lines), start_idx + 12)):
        line = lines[idx]
        match = THEOREM_NAME_RE.search(line)
        if match:
            return match.group(1)
        stripped = line.strip()
        if stripped and not stripped.startswith("--") and not stripped.startswith("/-"):
            continue
    return None


def collect_marker_hits(config: CleanupConfig) -> list[MarkerHit]:
    hits: list[MarkerHit] = []
    for path in iter_lean_files(config.project_root):
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        lines = text.splitlines()
        for idx, line in enumerate(lines):
            if TEMP_ATTR_RE.search(line):
                theorem_name = infer_theorem_name(lines, idx)
                hits.append(
                    MarkerHit(
                        path=path,
                        line=idx + 1,
                        theorem_name=theorem_name,
                    )
                )
    return hits


def format_hits(config: CleanupConfig, hits: list[MarkerHit]) -> str:
    items = []
    for hit in hits:
        rel = hit.path.relative_to(config.project_root)
        theorem_suffix = f" ({hit.theorem_name})" if hit.theorem_name else ""
        items.append(f"- {rel}:{hit.line}{theorem_suffix}")
    return "\n".join(items)


def run_command(config: CleanupConfig, args: list[str], description: str) -> None:
    result = subprocess.run(
        args,
        cwd=config.project_root,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
        raise CleanupFailure(f"{description} failed:\n{output}")


def run_audit(config: CleanupConfig) -> None:
    if config.skip_audit:
        return
    if not config.audit_script.exists():
        raise CleanupFailure(
            "Temporary axiom audit script is missing. "
            "Restore scripts/run_temporary_axiom_audit.sh or rerun with --skip-audit."
        )
    if not config.audit_modules:
        raise CleanupFailure(
            "Pre-cleanup audit requires at least one --audit-module.\n"
            "Pass your project root module or all relevant section modules, "
            "or rerun with --skip-audit if you intentionally want to bypass this check."
        )
    audit_rel = config.audit_script.relative_to(config.project_root)
    args = ["bash", str(audit_rel)]
    for module_name in config.audit_modules:
        args.extend(["--module", module_name])
    run_command(config, args, "Pre-cleanup temporary axiom audit")


def remove_delimited_blocks(text: str, start_marker: str, end_marker: str) -> tuple[str, int]:
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    idx = 0
    removed = 0
    while idx < len(lines):
        if lines[idx].strip() == start_marker:
            removed += 1
            idx += 1
            while idx < len(lines) and lines[idx].strip() != end_marker:
                idx += 1
            if idx == len(lines):
                raise CleanupFailure(f"Missing end marker {end_marker!r}")
            idx += 1
            continue
        out.append(lines[idx])
        idx += 1
    return "".join(out), removed


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def plan_remove_utility_imports(config: CleanupConfig) -> list[FileEdit]:
    import_prefix = f"import {config.utility_root_name}"
    edits: list[FileEdit] = []
    for path in iter_lean_files(config.project_root):
        if path == config.utility_root_file or is_within(path, config.utility_dir):
            continue
        old = path.read_text(encoding="utf-8")
        lines = old.splitlines(keepends=True)
        new_lines = [
            line
            for line in lines
            if not (
                line.strip() == import_prefix
                or line.strip().startswith(f"{import_prefix}.")
            )
        ]
        new = "".join(new_lines)
        if new != old:
            edits.append(FileEdit(path=path, new_text=new))
    return edits


def plan_workflow_updates(config: CleanupConfig) -> list[FileEdit]:
    edits: list[FileEdit] = []
    for path in config.workflow_files:
        if not path.exists():
            continue
        old = path.read_text(encoding="utf-8")
        new = old
        removed = 0
        for start_marker, end_marker in WORKFLOW_BLOCK_MARKERS:
            new, partial_removed = remove_delimited_blocks(new, start_marker, end_marker)
            removed += partial_removed
        if removed > 0:
            edits.append(FileEdit(path=path, new_text=new))
    return edits


def collect_unmanaged_workflow_refs(
    config: CleanupConfig, workflow_updates: list[FileEdit]
) -> list[WorkflowReferenceHit]:
    updated_texts = {edit.path: edit.new_text for edit in workflow_updates}
    hits: list[WorkflowReferenceHit] = []
    for path in config.workflow_files:
        if not path.exists():
            continue
        text = updated_texts.get(path)
        if text is None:
            text = path.read_text(encoding="utf-8")
        for idx, line in enumerate(text.splitlines(), start=1):
            for script_name in WORKFLOW_AUDIT_SCRIPT_HINTS:
                if script_name in line:
                    hits.append(
                        WorkflowReferenceHit(
                            path=path,
                            line=idx,
                            script_name=script_name,
                        )
                    )
    return hits


def format_unmanaged_workflow_refs(
    config: CleanupConfig, hits: list[WorkflowReferenceHit]
) -> str:
    items = []
    for hit in hits:
        start_marker, end_marker = WORKFLOW_AUDIT_SCRIPT_HINTS[hit.script_name]
        items.append(
            f"- {hit.path.relative_to(config.project_root)}:{hit.line} "
            f"references {hit.script_name}; wrap the CI block with "
            f"{start_marker} / {end_marker} or remove it manually"
        )
    return "\n".join(items)


def plan_readme_updates(config: CleanupConfig) -> list[FileEdit]:
    if config.keep_docs or not config.readme_file.exists():
        return []
    old = config.readme_file.read_text(encoding="utf-8")
    new, removed = remove_delimited_blocks(old, README_BLOCK_START, README_BLOCK_END)
    if removed == 0:
        return []
    return [FileEdit(path=config.readme_file, new_text=new)]


def remove_default_target(text: str, utility_root_name: str) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    changed = False
    new_lines: list[str] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        match = DEFAULT_TARGETS_RE.match(line.rstrip("\n"))
        if match is None:
            new_lines.append(line)
            idx += 1
            continue
        block_lines = [line]
        remainder = match.group(2)
        if "[" not in remainder:
            new_lines.append(line)
            idx += 1
            continue
        bracket_balance = remainder.count("[") - remainder.count("]")
        idx += 1
        while bracket_balance > 0 and idx < len(lines):
            block_lines.append(lines[idx])
            bracket_balance += lines[idx].count("[") - lines[idx].count("]")
            idx += 1
        if bracket_balance > 0:
            raise CleanupFailure("Unterminated defaultTargets array in lakefile.toml")
        block_text = "".join(block_lines)
        targets = re.findall(r'"([^"]+)"', block_text)
        if utility_root_name not in targets:
            new_lines.extend(block_lines)
            continue
        kept_targets = [target for target in targets if target != utility_root_name]
        changed = True
        if kept_targets:
            if len(block_lines) == 1:
                kept = ", ".join(f'"{target}"' for target in kept_targets)
                newline = "\n" if line.endswith("\n") else ""
                new_lines.append(f"{match.group(1)}[{kept}]{match.group(3)}{newline}")
            else:
                assignment_indent = re.match(r"^(\s*)", block_lines[0]).group(1)
                inner_indent = next(
                    (
                        re.match(r"^(\s*)", candidate).group(1)
                        for candidate in block_lines[1:]
                        if candidate.strip().startswith('"')
                    ),
                    assignment_indent + "  ",
                )
                new_lines.append(f"{match.group(1)}[\n")
                for target_idx, target in enumerate(kept_targets):
                    comma = "," if target_idx < len(kept_targets) - 1 else ""
                    new_lines.append(f'{inner_indent}"{target}"{comma}\n')
                new_lines.append(f"{assignment_indent}]\n")
    rebuilt_text = "".join(new_lines)
    return rebuilt_text, changed


def remove_lean_lib_blocks(text: str, utility_root_name: str) -> tuple[str, bool]:
    lines = text.splitlines()
    out: list[str] = []
    idx = 0
    changed = False
    while idx < len(lines):
        if lines[idx].strip() != "[[lean_lib]]":
            out.append(lines[idx])
            idx += 1
            continue
        start = idx
        idx += 1
        while idx < len(lines):
            stripped = lines[idx].strip()
            if stripped.startswith("[[") and stripped.endswith("]]"):
                break
            idx += 1
        block = lines[start:idx]
        block_name = None
        for line in block:
            match = LAKEFILE_NAME_RE.match(line)
            if match is not None:
                block_name = match.group(1)
                break
        if block_name == utility_root_name:
            changed = True
            continue
        out.extend(block)
    rebuilt_text = "\n".join(out)
    if text.endswith("\n") and rebuilt_text:
        rebuilt_text += "\n"
    return rebuilt_text, changed


def plan_lakefile_update(config: CleanupConfig) -> list[FileEdit]:
    if not config.lakefile.exists():
        return []
    old = config.lakefile.read_text(encoding="utf-8")
    new, changed_default_targets = remove_default_target(old, config.utility_root_name)
    new, changed_lean_lib = remove_lean_lib_blocks(new, config.utility_root_name)
    if not (changed_default_targets or changed_lean_lib):
        return []
    return [FileEdit(path=config.lakefile, new_text=new)]


def delete_paths(config: CleanupConfig) -> list[Path]:
    delete_candidates = [
        config.utility_root_file,
        config.utility_dir,
        config.audit_script,
        config.registry_audit_script,
        config.registry_tool_script,
    ]
    if not config.keep_docs:
        delete_candidates.extend(config.docs_files)
    existing = [path for path in delete_candidates if path.exists()]
    if config.registry_db_dir.exists():
        existing.append(config.registry_db_dir)
    return existing


def verify_build(config: CleanupConfig) -> None:
    if config.skip_build:
        return
    run_command(config, ["lake", "build"], "Post-cleanup lake build")


def merge_edits(*edit_groups: list[FileEdit]) -> list[FileEdit]:
    merged: dict[Path, FileEdit] = {}
    for group in edit_groups:
        for edit in group:
            merged[edit.path] = edit
    return sorted(merged.values(), key=lambda edit: edit.path.as_posix())


def backup_path_for(config: CleanupConfig, backup_root: Path, path: Path) -> Path:
    return backup_root / path.relative_to(config.project_root)


def restore_cleanup(
    config: CleanupConfig,
    edits: list[FileEdit],
    deleted_paths: list[Path],
    original_texts: dict[Path, str | None],
    backup_root: Path,
) -> None:
    for path in sorted(deleted_paths, key=lambda item: len(item.parts)):
        if path.exists():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        backup_path = backup_path_for(config, backup_root, path)
        if not backup_path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if backup_path.is_dir():
            shutil.copytree(backup_path, path)
        else:
            shutil.copy2(backup_path, path)
    for edit in edits:
        old_text = original_texts[edit.path]
        if old_text is None:
            if edit.path.exists():
                edit.path.unlink()
        else:
            edit.path.write_text(old_text, encoding="utf-8")
    shutil.rmtree(backup_root, ignore_errors=True)


def apply_cleanup(config: CleanupConfig, edits: list[FileEdit], deleted_paths: list[Path]) -> None:
    backup_root = Path(tempfile.mkdtemp(prefix="temporary_axiom_cleanup_"))
    original_texts: dict[Path, str | None] = {}
    try:
        for edit in edits:
            if edit.path.exists():
                original_texts[edit.path] = edit.path.read_text(encoding="utf-8")
            else:
                original_texts[edit.path] = None
            edit.path.parent.mkdir(parents=True, exist_ok=True)
            edit.path.write_text(edit.new_text, encoding="utf-8")

        for path in deleted_paths:
            backup_path = backup_path_for(config, backup_root, path)
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_dir():
                shutil.copytree(path, backup_path)
                shutil.rmtree(path)
            else:
                shutil.copy2(path, backup_path)
                path.unlink()

        verify_build(config)
    except BaseException as exc:
        restore_cleanup(config, edits, deleted_paths, original_texts, backup_root)
        raise CleanupFailure(f"{exc}\nCleanup changes were rolled back.") from exc
    else:
        shutil.rmtree(backup_root)


def print_plan(
    config: CleanupConfig,
    import_updates: list[FileEdit],
    workflow_updates: list[FileEdit],
    readme_updates: list[FileEdit],
    lakefile_updates: list[FileEdit],
    deleted_paths: list[Path],
) -> None:
    mode = "Applying" if config.execute else "Planned"
    print(f"{mode} temporary axiom scaffolding cleanup:")
    if import_updates:
        print("  Lean files with TemporaryAxiom imports removed:")
        for edit in import_updates:
            print(f"    - {edit.path.relative_to(config.project_root)}")
    if workflow_updates:
        print("  CI/CD workflow files with scaffolding audit blocks removed:")
        for edit in workflow_updates:
            print(f"    - {edit.path.relative_to(config.project_root)}")
    if readme_updates:
        print("  README blocks removed:")
        for edit in readme_updates:
            print(f"    - {edit.path.relative_to(config.project_root)}")
    if lakefile_updates:
        print("  Lake configuration updated:")
        for edit in lakefile_updates:
            print(f"    - {edit.path.relative_to(config.project_root)}")
    if deleted_paths:
        print("  Paths deleted:")
        for path in deleted_paths:
            print(f"    - {path.relative_to(config.project_root)}")
    if not import_updates and not workflow_updates and not readme_updates and not lakefile_updates and not deleted_paths:
        print("  No scaffold files or imports required cleanup.")


def main() -> None:
    config = parse_args()
    hits = collect_marker_hits(config)
    if hits:
        formatted = format_hits(config, hits)
        raise SystemExit(
            "Refusing to clean scaffolding because temporary_axiom markers remain:\n"
            f"{formatted}"
        )

    try:
        run_audit(config)
    except CleanupFailure as exc:
        raise SystemExit(str(exc))

    import_updates = plan_remove_utility_imports(config)
    workflow_updates = plan_workflow_updates(config)
    unmanaged_workflow_refs = collect_unmanaged_workflow_refs(config, workflow_updates)
    if unmanaged_workflow_refs:
        formatted = format_unmanaged_workflow_refs(config, unmanaged_workflow_refs)
        raise SystemExit(
            "Refusing to clean scaffolding because workflow audit steps would remain:\n"
            f"{formatted}"
        )
    readme_updates = plan_readme_updates(config)
    lakefile_updates = plan_lakefile_update(config)
    deleted_paths = delete_paths(config)
    all_edits = merge_edits(import_updates, workflow_updates, readme_updates, lakefile_updates)

    print_plan(config, import_updates, workflow_updates, readme_updates, lakefile_updates, deleted_paths)

    if config.execute:
        try:
            apply_cleanup(config, all_edits, deleted_paths)
        except CleanupFailure as exc:
            raise SystemExit(str(exc))
        print("Cleanup completed successfully.")


if __name__ == "__main__":
    main()
