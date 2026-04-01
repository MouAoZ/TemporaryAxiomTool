module

public import Lean

namespace TemporaryAxiomTool.ApprovedStatementRegistry

open Lean

private def binderInfoHash : BinderInfo → UInt64
  | .default => hash 0
  | .implicit => hash 1
  | .strictImplicit => hash 2
  | .instImplicit => hash 3

-- Universe 参数按首次出现编号，避免只改参数名就触发 hash 漂移。
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

-- 这里按 elaborated expr 的结构递归哈希；binder 名与 metadata 不进入结果。
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

这份 hash 是批准注册库与 `@[temporary_axiom]` 校验共用的唯一标准。
-/
public def statementHash (levelParams : List Name) (type : Expr) : UInt64 :=
  hashExprCore (mkLevelParamIndex levelParams) type

/-- 供 probe 命令与属性校验共享的 `ConstantInfo` 包装。 -/
public def statementHashOfConstInfo (constInfo : ConstantInfo) : UInt64 :=
  statementHash constInfo.levelParams constInfo.type

end TemporaryAxiomTool.ApprovedStatementRegistry
