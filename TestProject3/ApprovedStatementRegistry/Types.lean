/-
Shared data types for the generated approved-statement registry.

`Types.lean` stays separate so auto-generated shard modules can depend on a
small stable interface without importing the heavier root registry module.
-/
import Lean

namespace TestProject3.ApprovedStatementRegistry

open Lean

structure ApprovedStatement where
  name : Name
  shardId : String
  statementHash : UInt64
  deriving Inhabited, Repr

abbrev ApprovedStatementMap := NameMap ApprovedStatement

def insertApprovedStatements
    (registry : ApprovedStatementMap) (entries : Array ApprovedStatement) : ApprovedStatementMap :=
  entries.foldl (init := registry) fun acc entry => acc.insert entry.name entry

end TestProject3.ApprovedStatementRegistry
