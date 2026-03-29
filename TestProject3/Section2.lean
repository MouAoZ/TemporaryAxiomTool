import TestProject3.Section1

namespace TestProject3

section Section2

variable {P Q R : Prop}

theorem «lemma_2_1» (h : (P ∧ Q) ∧ R) : P ∧ Q := h.left

theorem «lemma_2_2» (h : (P ∧ Q) ∧ R) : R := h.right

theorem «lemma_2_3» (hR : R) (hQP : Q ∧ P) : R ∧ (Q ∧ P) := And.intro hR hQP

theorem SectionMainTheorem_2 (h : (P ∧ Q) ∧ R) : R ∧ (Q ∧ P) := by
  have hPQ : P ∧ Q := «lemma_2_1» h
  have hR : R := «lemma_2_2» h
  have hQP : Q ∧ P := SectionMainTheorem_1 hPQ
  exact «lemma_2_3» hR hQP

end Section2

end TestProject3
