module

public import Lean
public meta import Lean
public meta import Lean.Elab.Command
public meta import Lean.Elab.Term
public import TemporaryAxiomTool.StatementHash
public import TemporaryAxiomTool.PreparedSession.Types
import TemporaryAxiomTool.PreparedSession.Generated
public meta import TemporaryAxiomTool.StatementHash

open Lean Elab Command

namespace TemporaryAxiomTool.PreparedSession

private meta def moduleNameFor? (env : Environment) (declName : Name) : Option Name :=
  match env.getModuleIdxFor? declName with
  | some moduleIdx => env.header.moduleNames[moduleIdx.toNat]?
  | none => none

private meta def isSupportedTemporaryAxiomConstInfo (constInfo : ConstantInfo) : Bool :=
  match constInfo with
  | .thmInfo _ => true
  | .opaqueInfo _ => true
  | .axiomInfo _ => true
  | _ => false

private meta def implementationNameFor? (env : Environment) (declName : Name) : Option Name := Id.run do
  let mut found : Option Name := none
  for (candidateName, _) in env.constants do
    if let some userName := privateToUserName? candidateName then
      if userName == declName then
        found := some candidateName
        break
  found

private meta def effectiveConstInfoFor (env : Environment) (declName : Name) : CoreM ConstantInfo := do
  match implementationNameFor? env declName with
  | some implName =>
      getConstInfo implName
  | none =>
      getConstInfo declName

private meta def declProbeJson
    (declName : Name)
    (moduleName : Name)
    (statementHash : UInt64) : Json :=
  Json.mkObj [
    ("decl_name", Json.str <| toString declName),
    ("module", Json.str <| toString moduleName),
    ("statement_hash", Json.str <| toString statementHash.toNat)
  ]

/--
输出单个声明的探测结果，供 Python `prepare` 流程冻结 target 信息。

输入使用 name literal，而不是通过当前 namespace 解析 identifier，方便 Python
直接按 `.ilean` 中的声明全名批量发起 probe。
-/
elab "#print_temporary_axiom_decl_probe " quotedName:term : command => do
  let declName ← liftTermElabM do
    unsafe Term.evalTerm Name (mkConst ``Name) quotedName
  let env ← getEnv
  let some _ := env.find? declName | pure ()
  let constInfo ← liftCoreM <| effectiveConstInfoFor env declName
  let moduleName ← match moduleNameFor? env declName with
    | some moduleName => pure moduleName
    | none => pure Name.anonymous
  if moduleName == Name.anonymous then
    pure ()
  else if !isSupportedTemporaryAxiomConstInfo constInfo then
    pure ()
  else
    logInfo <|
      (declProbeJson
        declName
        moduleName
        (TemporaryAxiomTool.statementHashOfConstInfo constInfo)).compress

public def targetName : Name :=
  generatedTargetName

public def permittedAxioms : Array PermittedAxiom :=
  generatedPermittedAxioms

public def hasActiveSession : Bool :=
  targetName != Name.anonymous

public def permittedAxiomMap : PermittedAxiomMap :=
  insertPermittedAxioms {} permittedAxioms

end TemporaryAxiomTool.PreparedSession
