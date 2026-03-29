# Temporary Theorem Workflow

## Team Roles

This workflow assumes two different responsibilities:

- theorem owners define and eventually discharge the real proof
- downstream teams only depend on the stable theorem interface

The marker `@[temporary_axiom]` exists to make that boundary explicit.

## Upstream Workflow

When a theorem statement is stable but the proof is not ready:

1. keep the final theorem name and statement fixed
2. add `@[temporary_axiom]`
3. leave a parser-valid proof body in place
4. commit that small interface-stub change

Recommended shape:

```lean
@[temporary_axiom]
theorem SectionMainTheorem_2 (h : (P ∧ Q) ∧ R) : R ∧ (Q ∧ P) := by
  sorry
```

This commit should be small and dedicated. Other teams can cherry-pick or merge
it with minimal conflict risk.

## Downstream Workflow

Downstream teams should treat the marked declaration exactly like a frozen API:

- rely on the theorem name
- rely on the theorem statement
- do not edit the theorem body
- do not change the theorem header

In this repository,
[TestProject3/Section3.lean](/home/mouao/lean_projects/test_project3/TestProject3/Section3.lean)
continues to use `SectionMainTheorem_2` exactly as before.

## Merge Discipline

Before other teams begin depending on a marked theorem:

- the theorem owner freezes the statement
- the integration branch receives the explicit temporary-axiom commit

While parallel work continues:

- downstream branches only consume the frozen theorem interface
- proof development can continue on separate owner branches without disturbing
  shared integration branches

This reduces merge noise because the shared branch contains only the stable stub,
not a constantly changing partial proof.

## Recovery Workflow

When the real proof is ready:

1. update the proof body until it is genuinely complete
2. delete the `@[temporary_axiom]` line
3. run `lake build`
4. run `lake env lean TemporaryTheoremAudit.lean`
5. merge the recovery commit

The key signal is the attribute removal itself. No custom cleanup syntax is
required.

## Audit Workflow

During integration:

```lean
#print_temporary_axioms
```

At closure:

```lean
#assert_no_temporary_axioms
```

The recommended closure file is
[TemporaryTheoremAudit.lean](/home/mouao/lean_projects/test_project3/TemporaryTheoremAudit.lean).

## CI/CD Tutorial

To add closure auditing to CI/CD:

1. keep
   [TemporaryTheoremAudit.lean](/home/mouao/lean_projects/test_project3/TemporaryTheoremAudit.lean)
   in the repository
2. use
   [scripts/run_temporary_theorem_audit.sh](/home/mouao/lean_projects/test_project3/scripts/run_temporary_theorem_audit.sh)
   as the canonical audit command
3. wrap the CI/CD audit step in the exact marker comments below so automated cleanup
   can remove it later

Recommended GitHub Actions snippet:

```yaml
      # temporary-theorem-audit:start
      - name: Temporary theorem closure audit
        run: ./scripts/run_temporary_theorem_audit.sh
      # temporary-theorem-audit:end
```

Recommended local check before enabling the CI/CD gate:

```bash
./scripts/run_temporary_theorem_audit.sh
```

Only enable this gate on branches that are supposed to be free of
`@[temporary_axiom]`. If temporary axioms are still part of the current iteration,
leave the audit step out of the active workflow until closure time.

## Automated Cleanup Tool

This repository now includes
[scripts/cleanup_temporary_theorem_scaffolding.py](/home/mouao/lean_projects/test_project3/scripts/cleanup_temporary_theorem_scaffolding.py).

The tool serves two purposes:

- locate every remaining `@[temporary_axiom]` marker with file and line information
- once no markers remain, remove the temporary-theorem scaffolding in one pass

It intentionally does not try to judge whether unrelated proof debt remains. In
particular, it tracks `temporary_axiom` markers, not general `sorry` usage.

Default mode is a dry run:

```bash
python3 scripts/cleanup_temporary_theorem_scaffolding.py
```

Apply the cleanup:

```bash
python3 scripts/cleanup_temporary_theorem_scaffolding.py --execute
```

By default, cleanup removes:

- imports of `TemporaryTheorem`
- [TemporaryTheoremAudit.lean](/home/mouao/lean_projects/test_project3/TemporaryTheoremAudit.lean)
- [TestProject3/TemporaryTheorem.lean](/home/mouao/lean_projects/test_project3/TestProject3/TemporaryTheorem.lean)
- [scripts/run_temporary_theorem_audit.sh](/home/mouao/lean_projects/test_project3/scripts/run_temporary_theorem_audit.sh)
- CI/CD audit blocks wrapped by `# temporary-theorem-audit:start/end`
- temporary-theorem documentation files and the README doc block

Use `--keep-docs` if you want the historical documentation to remain in the repository.

## Final Cleanup

Once the whole project is free of marked declarations:

1. remove any leftover `@[temporary_axiom]` lines
2. verify the audit entrypoint passes
3. run `python3 scripts/cleanup_temporary_theorem_scaffolding.py`
4. if the plan looks correct, rerun with `--execute`

Separating proof completion from infrastructure cleanup keeps history easier to
review and revert.
