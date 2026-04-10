module

import TemporaryAxiomTool
import TemporaryAxiomTool.TheoremRegistry.Shards.TemporaryAxiomTool.TestFixture.DepA

namespace TemporaryAxiomTool.TestFixture.DepA

public theorem dep_sorry : True := by
  sorry

public theorem dep_done : True := by
  trivial

end TemporaryAxiomTool.TestFixture.DepA
