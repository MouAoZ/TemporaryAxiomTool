module

import TemporaryAxiomTool
import TemporaryAxiomTool.TestFixture.DepA
import TemporaryAxiomTool.TheoremRegistry.Shards.TemporaryAxiomTool.TestFixture.DepB

namespace TemporaryAxiomTool.TestFixture.DepB

public theorem chain_sorry : True := by
  sorry

public theorem chain_done : True := by
  trivial

end TemporaryAxiomTool.TestFixture.DepB
