import TestProject3

/-!
Closure audit entrypoint.

Compile this file when the integration branch is expected to be free of
`@[temporary_axiom]` declarations. Compilation fails if any remain.
-/

#assert_no_temporary_axioms
