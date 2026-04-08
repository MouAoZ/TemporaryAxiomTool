module

public import Lean
public meta import Lean
public meta import Lean.Elab.Command
public meta import Lean.Elab.Term
public import TemporaryAxiomTool.StatementHash
public import TemporaryAxiomTool.PreparedSession.Types
public meta import TemporaryAxiomTool.PreparedSession.Types
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

private def generatedTargetMarkerPrefix : String :=
  "TemporaryAxiomTool.PreparedSession.Target.target_decl_"

private def generatedPermittedModulePrefix : String :=
  "TemporaryAxiomTool.PreparedSession.Permitted."

private def generatedPermittedMarkerSegment : String :=
  ".permitted_decl_"

private def generatedPermittedHashSegment : String :=
  "__hash_"

private def encodedRuntimeStringPrefix : String :=
  "u_"

private def stripStringPrefix? (pre text : String) : Option String :=
  if text.startsWith pre then
    some (text.drop pre.length |>.toString)
  else
    none

private def decodeRuntimeStringComponent? (encoded : String) : Option String := do
  let payload ← stripStringPrefix? encodedRuntimeStringPrefix encoded
  if payload == "empty" then
    return ""
  let mut chars : Array Char := #[]
  for codeText in payload.splitOn "_" do
    if codeText.isEmpty then
      failure
    let codePoint ← codeText.toNat?
    chars := chars.push (Char.ofNat codePoint)
  return String.ofList chars.toList

private def parseGeneratedTargetDeclName? (constName : Name) : Option String := do
  let encodedDecl ← stripStringPrefix? generatedTargetMarkerPrefix (toString constName)
  decodeRuntimeStringComponent? encodedDecl

private def parseGeneratedPermittedAxiom? (constName : Name) : Option PermittedAxiom := do
  let constNameText := toString constName
  if !constNameText.startsWith generatedPermittedModulePrefix then
    failure
  match constNameText.splitOn generatedPermittedMarkerSegment with
  | [_modulePart, markerPayload] =>
      match markerPayload.splitOn generatedPermittedHashSegment with
      | [encodedDecl, statementHash] =>
          let declNameText ← decodeRuntimeStringComponent? encodedDecl
          let hashNat ← statementHash.toNat?
          some {
            declNameText := declNameText
            statementHash := hashNat.toUInt64
          }
      | _ => none
  | _ => none

public def targetDeclNameText? : CoreM (Option String) := do
  let env ← getEnv
  for (constName, _) in env.constants do
    if let some targetDeclName := parseGeneratedTargetDeclName? constName then
      return some targetDeclName
  return none

public def targetName : CoreM Name := do
  let some targetNameText ← targetDeclNameText? | return Name.anonymous
  if targetNameText.isEmpty then
    return Name.anonymous
  return targetNameText.toName

public def hasActiveSession : CoreM Bool := do
  return (← targetDeclNameText?).isSome

public def permittedAxiomMap : CoreM PermittedAxiomMap := do
  let env ← getEnv
  let mut result : PermittedAxiomMap := {}
  for (constName, _) in env.constants do
    let some entry := parseGeneratedPermittedAxiom? constName | continue
    result := result.insert entry.declNameText entry
  return result

public def permittedAxiomFor? (declName : Name) : CoreM (Option PermittedAxiom) := do
  return (← permittedAxiomMap).get? (toString declName)

end TemporaryAxiomTool.PreparedSession
