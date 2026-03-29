# Temporary Theorem Specification

## Goal

This project uses an explicit marker workflow for parallel formalization:

- theorem owners keep the final theorem name and statement stable
- downstream teams continue formalizing against that stable interface
- a marked theorem is compiled as an `axiom`, so unfinished proofs do not block
  later sections and do not emit `sorry` warnings

The marker is a single attribute line:

```lean
@[temporary_axiom]
theorem SectionMainTheorem_2 (h : (P ∧ Q) ∧ R) : R ∧ (Q ∧ P) := by
  sorry
```

With the marker present, the theorem body is ignored and the declaration is treated
as if it were written as:

```lean
@[temporary_axiom]
axiom SectionMainTheorem_2 (h : (P ∧ Q) ∧ R) : R ∧ (Q ∧ P)
```

Removing the attribute restores normal theorem elaboration without changing the
theorem header or indentation.

## Mechanism

Implementation lives in
[TestProject3/TemporaryTheorem.lean](/home/mouao/lean_projects/test_project3/TestProject3/TemporaryTheorem.lean).

The module provides:

- `@[temporary_axiom]`: a label attribute used for explicit marking and auditing
- a command macro that rewrites marked top-level theorems into axioms before proof elaboration
- `#print_temporary_axioms`
- `#assert_no_temporary_axioms`

## Conversion Invariant

The macro changes only the declaration kind:

- from `theorem`
- to `axiom`

It reuses the original:

- declaration modifiers
- declaration name
- universe parameter syntax
- binder list
- result type syntax

This is deliberate. Lean's built-in `axiom` elaborator remains responsible for:

- namespace expansion
- private/public handling
- protected declarations
- auto-bound implicits
- section variables
- universe parameter checking
- other declaration attributes

Because the implementation only swaps the declaration head and then hands the
result back to Lean's normal declaration pipeline, parameter handling and
namespace handling stay aligned with native Lean behavior.

## Supported Syntax

Version 1 supports top-level declarations of the form:

```lean
@[temporary_axiom]
theorem Name.{u_1, ..., u_n} (binders) : Type := by
  ...
```

Practical support includes:

- doc comments
- multiple declaration attributes
- `private` / `public`
- `protected`
- explicit and implicit binders
- instance-implicit binders
- namespaced declaration names such as `Foo.Bar.baz`

## Non-Goals For Version 1

The workflow is intentionally narrow. It is not intended to support:

- `mutual` theorem blocks
- `example`
- non-theorem declarations
- parser-invalid proof bodies

The proof body may still contain unfinished proof text such as `sorry`, but it
must remain parser-valid so that Lean can parse the theorem command before the
macro rewrites it.

## Auditing

The environment audit commands are:

```lean
#print_temporary_axioms
#assert_no_temporary_axioms
```

Use
[TemporaryTheoremAudit.lean](/home/mouao/lean_projects/test_project3/TemporaryTheoremAudit.lean)
as a closure entrypoint for integration branches or CI.

The repository also provides a thin runner script:

- [scripts/run_temporary_theorem_audit.sh](/home/mouao/lean_projects/test_project3/scripts/run_temporary_theorem_audit.sh)

## CI/CD Integration

For GitHub Actions or similar CI/CD systems, the simplest integration is:

1. keep
   [TemporaryTheoremAudit.lean](/home/mouao/lean_projects/test_project3/TemporaryTheoremAudit.lean)
   in the repository
2. add a dedicated audit step after the normal Lean build
3. wrap the step in cleanup markers so the cleanup script can remove it later

Recommended GitHub Actions block:

```yaml
      # temporary-theorem-audit:start
      - name: Temporary theorem closure audit
        run: ./scripts/run_temporary_theorem_audit.sh
      # temporary-theorem-audit:end
```

Recommended insertion point in
[.github/workflows/lean_action_ci.yml](/home/mouao/lean_projects/test_project3/.github/workflows/lean_action_ci.yml):

1. keep the existing checkout step
2. keep the existing `lean-action` build step
3. append the audit block immediately after the build step

If the branch still contains temporary axioms, this audit step should be omitted or
kept disabled, because the purpose of the closure audit is to fail once the project
claims it is free of temporary scaffolding.

For non-GitHub CI/CD systems, the portable command is:

```bash
./scripts/run_temporary_theorem_audit.sh
```

The shell wrapper exists so that CI and local verification use the same entrypoint.

## Cleanup Contract

`@[temporary_axiom]` is the only switch.

- If the attribute is present, the declaration is compiled as an axiom.
- If the attribute is absent, the declaration is compiled as a normal theorem.

That gives a simple cleanup rule:

1. finish the actual proof
2. delete the `@[temporary_axiom]` line
3. run `lake build`
4. run the audit entrypoint

For full infrastructure cleanup after all markers are gone, use
[scripts/cleanup_temporary_theorem_scaffolding.py](/home/mouao/lean_projects/test_project3/scripts/cleanup_temporary_theorem_scaffolding.py).
The script is intentionally scoped to `temporary_axiom` markers and related
scaffolding; it does not try to certify the absence of unrelated `sorry` usage or
other proof debt.

## Migration To Another Lean Project

To reuse this workflow in another repository:

1. copy
   [TestProject3/TemporaryTheorem.lean](/home/mouao/lean_projects/test_project3/TestProject3/TemporaryTheorem.lean)
   into the target project
2. rename the module path to match the target project's namespace
3. import the module where temporary theorem stubs are needed
4. optionally copy
   [TemporaryTheoremAudit.lean](/home/mouao/lean_projects/test_project3/TemporaryTheoremAudit.lean)
   and adapt the root import
5. optionally copy
   [scripts/run_temporary_theorem_audit.sh](/home/mouao/lean_projects/test_project3/scripts/run_temporary_theorem_audit.sh)
   and
   [scripts/cleanup_temporary_theorem_scaffolding.py](/home/mouao/lean_projects/test_project3/scripts/cleanup_temporary_theorem_scaffolding.py)
   if the target project wants the same CI/CD and cleanup workflow

The implementation depends only on `import Lean`, which keeps migration fast and
keeps the workflow independent of project-specific theorem libraries.
