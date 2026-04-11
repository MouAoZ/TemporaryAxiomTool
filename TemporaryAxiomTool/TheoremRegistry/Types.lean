module

public import Lean

namespace TemporaryAxiomTool.TheoremRegistry

open Lean

public inductive ModuleShardMode where
  | inactive
  | collect
  | active
  deriving Inhabited, Repr, BEq

public structure RegisteredTheorem where
  name : Name
  statementHash : UInt64
  deriving Inhabited, Repr

public abbrev RegisteredTheoremBatch := Array RegisteredTheorem

public abbrev RegisteredTheoremMap := NameMap RegisteredTheorem

public def insertRegisteredTheoremBatch
    (entriesMap : RegisteredTheoremMap)
    (entries : RegisteredTheoremBatch) : RegisteredTheoremMap :=
  entries.foldl (init := entriesMap) fun acc entry => acc.insert entry.name entry

public structure ModuleShard where
  hostModule : Name
  mode : ModuleShardMode := .inactive
  targetName : Name := Name.anonymous
  targetHash : UInt64 := 0
  permitted : RegisteredTheoremMap := {}
  deriving Inhabited

public def ModuleShard.hasActiveSession (shard : ModuleShard) : Bool :=
  shard.mode == .active

public def ModuleShard.inCollectMode (shard : ModuleShard) : Bool :=
  shard.mode == .collect

public def ModuleShard.permittedFor? (shard : ModuleShard) (declName : Name) : Option RegisteredTheorem :=
  shard.permitted.find? declName

end TemporaryAxiomTool.TheoremRegistry
