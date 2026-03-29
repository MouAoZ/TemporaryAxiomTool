namespace TestProject3

section Section1

variable {P Q : Prop}

theorem «lemma_1_1» (h : P ∧ Q) : P := h.left

theorem «lemma_1_2» (h : P ∧ Q) : Q := h.right

theorem «lemma_1_3» (hP : P) (hQ : Q) : Q ∧ P := And.intro hQ hP

theorem SectionMainTheorem_1 (h : P ∧ Q) : Q ∧ P := by
  have hP : P := «lemma_1_1» h
  have hQ : Q := «lemma_1_2» h
  exact «lemma_1_3» hP hQ

end Section1

end TestProject3
