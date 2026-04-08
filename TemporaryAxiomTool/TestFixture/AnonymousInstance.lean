module

namespace TemporaryAxiomTool.TestFixture.AnonymousInstance

structure Wrap where
  val : Nat

instance : CoeFun Wrap (fun _ => Nat) := ⟨fun w => w.val⟩

def hidden : True := by
  sorry

public theorem helper : True := by
  sorry

public theorem goal : True := by
  sorry

end TemporaryAxiomTool.TestFixture.AnonymousInstance
