module

/-
自动生成的批准注册表分片共享的数据类型。

这层只保留 Lean 运行时真正消费的字段，让生成文件依赖稳定且轻量。
-/
public import Lean

namespace TemporaryAxiomTool.ApprovedStatementRegistry

open Lean

/-- Lean 运行时视角下的一条已批准陈述。 -/
public structure ApprovedStatement where
  name : Name
  shardId : String
  statementHash : UInt64
  deriving Inhabited, Repr

public abbrev ApprovedStatementMap := NameMap ApprovedStatement

-- 生成文件只负责给出数组，这里统一折成查找表。
public def insertApprovedStatements
    (registry : ApprovedStatementMap) (entries : Array ApprovedStatement) : ApprovedStatementMap :=
  entries.foldl (init := registry) fun acc entry => acc.insert entry.name entry

end TemporaryAxiomTool.ApprovedStatementRegistry
