/- 
已批准陈述注册库的 Lean 侧根模块。

这是由 `TemporaryAxiom` 直接依赖的唯一手写入口，负责：

- 定义 statement hash 算法
- 提供离线探测命令 `#print_approved_statement_probe`
- 把自动生成的 shard 数据装配成查找表
-/
import Lean
import TemporaryAxiomTool.ApprovedStatementRegistry.Types
import TemporaryAxiomTool.ApprovedStatementRegistry.Generated

open Lean Elab Command Meta

namespace TemporaryAxiomTool.ApprovedStatementRegistry

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
对声明的 elaborated type 做稳定哈希。

这份 hash 是批准注册库与 `@[temporary_axiom]` 校验共同使用的唯一标准。
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
提供给 Python 管理脚本的离线探测命令。

它会读取目标声明的最终类型，并输出一行 JSON，供外部注册库冻结该陈述。
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

end TemporaryAxiomTool.ApprovedStatementRegistry
