module

public import Lean

namespace TemporaryAxiomTool.PreparedSession

open Lean

/-- Lean 运行时视角下的一条当前 session 允许的临时公理条目。 -/
public structure PermittedAxiom where
  name : Name
  statementHash : UInt64
  deriving Inhabited, Repr

public abbrev PermittedAxiomBatch := Array PermittedAxiom

public abbrev PermittedAxiomMap := NameMap PermittedAxiom

public def insertPermittedAxioms
    (entriesMap : PermittedAxiomMap)
    (entries : PermittedAxiomBatch) : PermittedAxiomMap :=
  entries.foldl (init := entriesMap) fun acc entry => acc.insert entry.name entry

end TemporaryAxiomTool.PreparedSession
