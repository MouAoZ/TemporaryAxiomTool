module

namespace TemporaryAxiomTool.TestFixture.ComplexAttr

/-- The helper carries a tail comment on its attribute line. -/
@[simp] -- keep the tail comment untouched
public theorem helper : True := by
  sorry

/-- The second helper uses a multi-line attribute block. -/
@[
  simp
]
public theorem helper2 : True := by
  sorry

public theorem goal : True := by
  sorry

end TemporaryAxiomTool.TestFixture.ComplexAttr
