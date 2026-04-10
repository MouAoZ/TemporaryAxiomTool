module

import TemporaryAxiomTool
import TemporaryAxiomTool.TestFixture.DepB
import TemporaryAxiomTool.TheoremRegistry.Shards.TemporaryAxiomTool.TestFixture.Target

namespace TemporaryAxiomTool.TestFixture.Target

/-- Local unresolved theorem before the target. -/
public theorem local_sorry : True := by
  sorry

@[simp] public theorem local_attr_sorry : True := by
  sorry

public theorem local_done : True := by
  trivial

public theorem goal : True := by
  sorry

public theorem later_sorry : True := by
  sorry

end TemporaryAxiomTool.TestFixture.Target
