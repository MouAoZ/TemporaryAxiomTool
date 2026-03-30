/-
Approved statement registry root module.

This is the only manually maintained registry entrypoint imported by
`TemporaryAxiom`. It contains:

- the statement-hash implementation used for declaration validation
- the offline `#print_approved_statement_probe` command used by the Python tool
- the assembly step that turns generated shard data into a lookup map
-/
import Lean
import TestProject3.ApprovedStatementRegistry.Types
import TestProject3.ApprovedStatementRegistry.Generated

open Lean Elab Command Meta

namespace TestProject3.ApprovedStatementRegistry

private def binderInfoHash : BinderInfo → UInt64
  | .default => hash 0
  | .implicit => hash 1
  | .strictImplicit => hash 2
  | .instImplicit => hash 3

private def mkLevelParamIndex (levelParams : List Name) : NameMap Nat :=
  let (_, index) := levelParams.foldl (init := (0, ({} : NameMap Nat))) fun (nextIdx, acc) levelName =>
    if acc.contains levelName then
      (nextIdx, acc)
    else
      (nextIdx + 1, acc.insert levelName nextIdx)
  index

private partial def hashLevel (levelParamIndex : NameMap Nat) : Level → UInt64
  | .zero => mixHash (hash 0) 0
  | .succ u => mixHash (hash 1) (hashLevel levelParamIndex u)
  | .max u v =>
      mixHash (hash 2) <| mixHash (hashLevel levelParamIndex u) (hashLevel levelParamIndex v)
  | .imax u v =>
      mixHash (hash 3) <| mixHash (hashLevel levelParamIndex u) (hashLevel levelParamIndex v)
  | .param name =>
      let idxHash := hash <| match levelParamIndex.find? name with
        | some idx => idx
        | none => name.hash.toNat
      mixHash (hash 4) idxHash
  | .mvar mvarId => mixHash (hash 5) (hash mvarId.name)

private partial def hashExprCore (levelParamIndex : NameMap Nat) : Expr → UInt64
  | .bvar idx => mixHash (hash 10) (hash idx)
  | .fvar fvarId => mixHash (hash 11) (hash fvarId.name)
  | .mvar mvarId => mixHash (hash 12) (hash mvarId.name)
  | .sort level => mixHash (hash 13) (hashLevel levelParamIndex level)
  | .const declName us =>
      let levelsHash := us.foldl (init := hash 0) fun acc level =>
        mixHash acc (hashLevel levelParamIndex level)
      mixHash (hash 14) <| mixHash (hash declName) levelsHash
  | .app fn arg =>
      mixHash (hash 15) <| mixHash (hashExprCore levelParamIndex fn) (hashExprCore levelParamIndex arg)
  | .lam _ domain body binderInfo =>
      let domainHash := hashExprCore levelParamIndex domain
      let bodyHash := hashExprCore levelParamIndex body
      mixHash (hash 16) <| mixHash (binderInfoHash binderInfo) <| mixHash domainHash bodyHash
  | .forallE _ domain body binderInfo =>
      let domainHash := hashExprCore levelParamIndex domain
      let bodyHash := hashExprCore levelParamIndex body
      mixHash (hash 17) <| mixHash (binderInfoHash binderInfo) <| mixHash domainHash bodyHash
  | .letE _ type value body _ =>
      let typeHash := hashExprCore levelParamIndex type
      let valueHash := hashExprCore levelParamIndex value
      let bodyHash := hashExprCore levelParamIndex body
      mixHash (hash 18) <| mixHash typeHash <| mixHash valueHash bodyHash
  | .lit lit => mixHash (hash 19) (hash lit)
  | .mdata _ body => hashExprCore levelParamIndex body
  | .proj structName idx body =>
      mixHash (hash 20) <| mixHash (hash structName) <| mixHash (hash idx) (hashExprCore levelParamIndex body)

/--
Hash the elaborated type of a declaration. This is the canonical statement hash
used by both the generated registry and the `@[temporary_axiom]` validator.
-/
def statementHash (levelParams : List Name) (type : Expr) : UInt64 :=
  hashExprCore (mkLevelParamIndex levelParams) type

def statementHashOfConstInfo (constInfo : ConstantInfo) : UInt64 :=
  statementHash constInfo.levelParams constInfo.type

private def probeJson (declName : Name) (statementHash : UInt64) (statementPretty : String) : Json :=
  Json.mkObj [
    ("decl_name", Json.str <| toString declName),
    ("statement_hash", Json.str <| toString statementHash.toNat),
    ("statement_pretty", Json.str statementPretty)
  ]

/--
Offline probe command used by the Python registry manager. It elaborates the
target declaration and prints one JSON line describing the frozen statement.
-/
elab "#print_approved_statement_probe " id:ident : command => do
  let declName ← liftCoreM <| Elab.realizeGlobalConstNoOverloadWithInfo id
  let constInfo ← liftCoreM <| getConstInfo declName
  let ppType ← liftTermElabM do
    let fmt ← Meta.ppExpr constInfo.type
    pure fmt.pretty
  logInfo <| probeJson declName (statementHashOfConstInfo constInfo) ppType |>.compress

def approvedStatements : Array ApprovedStatement :=
  generatedApprovedStatements

def approvedStatementMap : ApprovedStatementMap :=
  insertApprovedStatements {} approvedStatements

end TestProject3.ApprovedStatementRegistry
