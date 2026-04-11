module

import TemporaryAxiomTool
import TemporaryAxiomTool.TestFixture.UntrackedDep
import TemporaryAxiomTool.TheoremRegistry.Shards.TemporaryAxiomTool.TestFixture.UntrackedTarget

namespace TemporaryAxiomTool.TestFixture.UntrackedTarget

public theorem goal : True := by
  exact TemporaryAxiomTool.TestFixture.UntrackedDep.dep_sorry

end TemporaryAxiomTool.TestFixture.UntrackedTarget
