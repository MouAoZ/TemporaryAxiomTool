module

public import Lean

namespace TemporaryAxiomTool.PreparedSession

open Lean

/-- Lean 运行时视角下的一条当前 session 允许的临时公理条目。 -/
public structure PermittedAxiom where
  declNameText : String
  statementHash : UInt64
  deriving Inhabited, Repr

public abbrev PermittedAxiomMap := Std.HashMap String PermittedAxiom

public def insertPermittedAxioms
    (entriesMap : PermittedAxiomMap)
    (entries : Array PermittedAxiom) : PermittedAxiomMap :=
  entries.foldl (init := entriesMap) fun acc entry => acc.insert entry.declNameText entry

end TemporaryAxiomTool.PreparedSession
