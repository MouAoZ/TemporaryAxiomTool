#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


CI_BLOCK_START = "# temporary-theorem-audit:start"
CI_BLOCK_END = "# temporary-theorem-audit:end"
README_BLOCK_START = "<!-- temporary-theorem-docs:start -->"
README_BLOCK_END = "<!-- temporary-theorem-docs:end -->"
TEMP_ATTR_RE = re.compile(r"^\s*@\[[^\]]*\btemporary_axiom\b[^\]]*\]")
THEOREM_NAME_RE = re.compile(r"\btheorem\s+([^\s:(]+)")


@dataclass(frozen=True)
class MarkerHit:
    path: Path
    line: int
    theorem_name: str | None


@dataclass(frozen=True)
class CleanupConfig:
    project_root: Path
    utility_module: str
    utility_file: Path
    audit_file: Path
    audit_script: Path
    workflow_files: tuple[Path, ...]
    readme_file: Path
    docs_files: tuple[Path, ...]
    execute: bool
    keep_docs: bool
    skip_audit: bool
    skip_build: bool


def parse_args() -> CleanupConfig:
    parser = argparse.ArgumentParser(
        description=(
            "Locate remaining temporary_axiom declarations and, once none remain, "
            "clean the temporary-theorem scaffolding from the project."
        )
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Path to the Lean project root. Defaults to the current directory.",
    )
    parser.add_argument(
        "--utility-module",
        default="TestProject3.TemporaryTheorem",
        help="Fully qualified module name for the temporary theorem utility.",
    )
    parser.add_argument(
        "--audit-file",
        default="TemporaryTheoremAudit.lean",
        help="Audit Lean file that runs #assert_no_temporary_axioms.",
    )
    parser.add_argument(
        "--audit-script",
        default="scripts/run_temporary_theorem_audit.sh",
        help="Shell script used by CI/CD to run the audit.",
    )
    parser.add_argument(
        "--workflow-file",
        action="append",
        default=[".github/workflows/lean_action_ci.yml"],
        help=(
            "Workflow file containing removable CI audit blocks delimited by "
            f"{CI_BLOCK_START!r} and {CI_BLOCK_END!r}. Can be repeated."
        ),
    )
    parser.add_argument(
        "--readme-file",
        default="README.md",
        help="README file containing the removable temporary-theorem docs block.",
    )
    parser.add_argument(
        "--keep-docs",
        action="store_true",
        help="Keep the temporary-theorem documentation files instead of deleting them.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply cleanup changes. Without this flag, the script only prints the plan.",
    )
    parser.add_argument(
        "--skip-audit",
        action="store_true",
        help="Skip the pre-cleanup TemporaryTheoremAudit.lean check.",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Skip the post-cleanup lake build verification step.",
    )
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    utility_file = project_root / (args.utility_module.replace(".", "/") + ".lean")

    return CleanupConfig(
        project_root=project_root,
        utility_module=args.utility_module,
        utility_file=utility_file,
        audit_file=project_root / args.audit_file,
        audit_script=project_root / args.audit_script,
        workflow_files=tuple(project_root / path for path in args.workflow_file),
        readme_file=project_root / args.readme_file,
        docs_files=(
            project_root / "docs/temporary_theorem_spec.md",
            project_root / "docs/temporary_theorem_workflow.md",
        ),
        execute=args.execute,
        keep_docs=args.keep_docs,
        skip_audit=args.skip_audit,
        skip_build=args.skip_build,
    )


def iter_lean_files(project_root: Path) -> list[Path]:
    excluded_roots = {".git", ".lake"}
    results: list[Path] = []
    for path in project_root.rglob("*.lean"):
        rel_parts = path.relative_to(project_root).parts
        if rel_parts and rel_parts[0] in excluded_roots:
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
        text = path.read_text(encoding="utf-8")
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
        raise SystemExit(f"{description} failed:\n{output}")


def run_audit(config: CleanupConfig) -> None:
    if config.skip_audit or not config.audit_file.exists():
        return
    audit_rel = config.audit_file.relative_to(config.project_root)
    run_command(
        config,
        ["lake", "env", "lean", str(audit_rel)],
        f"Audit {audit_rel}",
    )


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
                raise ValueError(f"Missing end marker {end_marker!r}")
            idx += 1
            continue
        out.append(lines[idx])
        idx += 1
    return ("".join(out), removed)


def cleanup_workflow_blocks(config: CleanupConfig) -> list[Path]:
    changed: list[Path] = []
    for path in config.workflow_files:
        if not path.exists():
            continue
        old = path.read_text(encoding="utf-8")
        new, removed = remove_delimited_blocks(old, CI_BLOCK_START, CI_BLOCK_END)
        if removed > 0:
            if config.execute:
                path.write_text(new, encoding="utf-8")
            changed.append(path)
    return changed


def cleanup_readme_block(config: CleanupConfig) -> list[Path]:
    if config.keep_docs or not config.readme_file.exists():
        return []
    old = config.readme_file.read_text(encoding="utf-8")
    new, removed = remove_delimited_blocks(old, README_BLOCK_START, README_BLOCK_END)
    if removed == 0:
        return []
    if config.execute:
        config.readme_file.write_text(new, encoding="utf-8")
    return [config.readme_file]


def remove_utility_imports(config: CleanupConfig) -> list[Path]:
    import_line = f"import {config.utility_module}"
    changed: list[Path] = []
    for path in iter_lean_files(config.project_root):
        if path in {config.utility_file, config.audit_file}:
            continue
        old = path.read_text(encoding="utf-8")
        lines = old.splitlines(keepends=True)
        new_lines = [line for line in lines if line.strip() != import_line]
        new = "".join(new_lines)
        if new != old:
            if config.execute:
                path.write_text(new, encoding="utf-8")
            changed.append(path)
    return changed


def delete_paths(config: CleanupConfig) -> list[Path]:
    delete_candidates = [
        config.utility_file,
        config.audit_file,
        config.audit_script,
    ]
    if not config.keep_docs:
        delete_candidates.extend(config.docs_files)

    existing = [path for path in delete_candidates if path.exists()]
    if config.execute:
        for path in existing:
            path.unlink()
    return existing


def verify_build(config: CleanupConfig) -> None:
    if config.skip_build:
        return
    run_command(config, ["lake", "build"], "Post-cleanup lake build")


def print_plan(
    config: CleanupConfig,
    import_updates: list[Path],
    workflow_updates: list[Path],
    readme_updates: list[Path],
    deleted_paths: list[Path],
) -> None:
    mode = "Applying" if config.execute else "Planned"
    print(f"{mode} temporary theorem scaffolding cleanup:")
    if import_updates:
        print("  Lean files with TemporaryTheorem imports removed:")
        for path in import_updates:
            print(f"    - {path.relative_to(config.project_root)}")
    if workflow_updates:
        print("  CI/CD workflow files with temporary-theorem audit blocks removed:")
        for path in workflow_updates:
            print(f"    - {path.relative_to(config.project_root)}")
    if readme_updates:
        print("  README blocks removed:")
        for path in readme_updates:
            print(f"    - {path.relative_to(config.project_root)}")
    if deleted_paths:
        print("  Files deleted:")
        for path in deleted_paths:
            print(f"    - {path.relative_to(config.project_root)}")
    if not import_updates and not workflow_updates and not readme_updates and not deleted_paths:
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

    run_audit(config)

    import_updates = remove_utility_imports(config)
    workflow_updates = cleanup_workflow_blocks(config)
    readme_updates = cleanup_readme_block(config)
    deleted_paths = delete_paths(config)
    print_plan(config, import_updates, workflow_updates, readme_updates, deleted_paths)

    if config.execute:
        verify_build(config)
        print("Cleanup completed successfully.")


if __name__ == "__main__":
    main()
