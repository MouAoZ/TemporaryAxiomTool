module

import TemporaryAxiomTool
import TemporaryAxiomTool.TheoremRegistry.Shards.TemporaryAxiomTool.TestFixture.PrivateReplay

namespace TemporaryAxiomTool.TestFixture.PrivateReplay

private theorem helper_done : True := by
  trivial

private theorem helper_sorry : True := by
  sorry

public theorem goal : True := by
  have _ : True := helper_done
  exact helper_sorry

end TemporaryAxiomTool.TestFixture.PrivateReplay
