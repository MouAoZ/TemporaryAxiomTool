import TestProject3.Section1
import TestProject3.TemporaryTheorem

namespace TestProject3

section Section2

variable {P Q R : Prop}

theorem «lemma_2_1» (h : (P ∧ Q) ∧ R) : P ∧ Q := h.left

theorem «lemma_2_2» (h : (P ∧ Q) ∧ R) : R := h.right

theorem «lemma_2_3» (hR : R) (hQP : Q ∧ P) : R ∧ (Q ∧ P) := And.intro hR hQP

@[temporary_axiom]
theorem SectionMainTheorem_2 (h : (P ∧ Q) ∧ R) : R ∧ (Q ∧ P) := by
  {}

end Section2

end TestProject3
