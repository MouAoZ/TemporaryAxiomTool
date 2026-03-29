import TestProject3.Section3

namespace TestProject3

theorem FinalTheorem {P Q R S : Prop} (h : ((P ∧ Q) ∧ R) ∧ S) :
    S ∧ (R ∧ (Q ∧ P)) := by
  exact SectionMainTheorem_3 h

end TestProject3
