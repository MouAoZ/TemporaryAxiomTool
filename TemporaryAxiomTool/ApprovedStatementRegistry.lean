module

/-
已批准陈述注册库的 Lean 侧手写入口。

这里保留探测命令和运行时查找表装配；statement hash 逻辑单独放在
`Hash.lean`，让生成文件和元编程入口共用同一份 ordinary 实现。
-/
public import Lean
public meta import Lean.Elab.Command
public meta import Lean.Meta
public import TemporaryAxiomTool.ApprovedStatementRegistry.Types
public import TemporaryAxiomTool.ApprovedStatementRegistry.Hash
import TemporaryAxiomTool.ApprovedStatementRegistry.Generated
public meta import TemporaryAxiomTool.ApprovedStatementRegistry.Hash

open Lean Elab Command Meta

namespace TemporaryAxiomTool.ApprovedStatementRegistry

-- 这里的字段名直接对齐 Python 侧 probe 解析结果，避免再做一层映射。
private meta def probeJson (declName : Name) (statementHash : UInt64) (statementPretty : String) : Json :=
  Json.mkObj [
    ("decl_name", Json.str <| toString declName),
    ("statement_hash", Json.str <| toString statementHash.toNat),
    ("statement_pretty", Json.str statementPretty)
  ]

/--
提供给 Python 管理脚本的离线探测命令。

它读取目标声明的最终类型，并输出一行 JSON，供外部注册库冻结该陈述。
-/
elab "#print_approved_statement_probe " id:ident : command => do
  let declName ← liftCoreM <| Elab.realizeGlobalConstNoOverloadWithInfo id
  let constInfo ← liftCoreM <| getConstInfo declName
  let ppType ← liftTermElabM do
    let fmt ← Meta.ppExpr constInfo.type
    pure fmt.pretty
  logInfo <| probeJson declName (statementHashOfConstInfo constInfo) ppType |>.compress

public def approvedStatements : Array ApprovedStatement :=
  generatedApprovedStatements

-- 运行时把生成数组一次性折成 `NameMap`，属性校验时只做查表。
public def approvedStatementMap : ApprovedStatementMap :=
  insertApprovedStatements {} approvedStatements

end TemporaryAxiomTool.ApprovedStatementRegistry
