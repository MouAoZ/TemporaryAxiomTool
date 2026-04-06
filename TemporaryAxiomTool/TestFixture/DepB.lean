module

import TemporaryAxiomTool.TestFixture.DepA

namespace TemporaryAxiomTool.TestFixture.DepB

public theorem chain_sorry : True := by
  sorry

public theorem chain_done : True := by
  trivial

end TemporaryAxiomTool.TestFixture.DepB
