import TestProject3.Section2

namespace TestProject3

section Section3

variable {P Q R S : Prop}

theorem «lemma_3_1» (h : ((P ∧ Q) ∧ R) ∧ S) : (P ∧ Q) ∧ R := h.left

theorem «lemma_3_2» (h : ((P ∧ Q) ∧ R) ∧ S) : S := h.right

theorem «lemma_3_3» (hS : S) (hRQP : R ∧ (Q ∧ P)) : S ∧ (R ∧ (Q ∧ P)) :=
  And.intro hS hRQP

theorem SectionMainTheorem_3 (h : ((P ∧ Q) ∧ R) ∧ S) : S ∧ (R ∧ (Q ∧ P)) := by
  have hPQR : (P ∧ Q) ∧ R := «lemma_3_1» h
  have hS : S := «lemma_3_2» h
  have hRQP : R ∧ (Q ∧ P) := SectionMainTheorem_2 hPQR
  exact «lemma_3_3» hS hRQP

end Section3

end TestProject3
