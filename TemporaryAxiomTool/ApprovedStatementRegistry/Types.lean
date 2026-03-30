/- 
自动生成的批准注册表分片共享的数据类型。

`Types.lean` 与根模块分离，是为了让生成文件只依赖一个稳定且轻量的接口。
-/
import Lean

namespace TemporaryAxiomTool.ApprovedStatementRegistry

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

end TemporaryAxiomTool.ApprovedStatementRegistry
