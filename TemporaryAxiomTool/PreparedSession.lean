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

private def parseGeneratedTargetDeclName? (declName : Name) : Option Name := do
  let encodedDecl ← stripStringPrefix? generatedTargetMarkerPrefix (toString declName)
  let declNameText ← decodeRuntimeStringComponent? encodedDecl
  return declNameText.toName

private def parseGeneratedPermittedAxiom? (declName : Name) : Option PermittedAxiom := do
  let constNameText := toString declName
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

public initialize targetRuntimeExt : SimplePersistentEnvExtension Name (Option Name) ←
  registerSimplePersistentEnvExtension {
    name := `temporaryAxiomTargetRuntimeExt
    addEntryFn := fun _ declName => some declName
    addImportedFn := fun importedEntries =>
      importedEntries.foldl (init := none) fun state entries =>
        entries.foldl (init := state) fun _ declName => some declName
  }

public initialize permittedAxiomRuntimeExt : SimplePersistentEnvExtension PermittedAxiom PermittedAxiomMap ←
  registerSimplePersistentEnvExtension {
    name := `temporaryAxiomPermittedRuntimeExt
    addEntryFn := fun state entry => state.insert entry.declNameText entry
    addImportedFn := fun importedEntries =>
      importedEntries.foldl (init := {}) fun state entries =>
        entries.foldl (init := state) fun state entry => state.insert entry.declNameText entry
    replay? := some <|
      SimplePersistentEnvExtension.replayOfFilter
        (fun state entry => !state.contains entry.declNameText)
        (fun state entry => state.insert entry.declNameText entry)
  }

public initialize targetRuntimeAttr : Unit ←
  registerBuiltinAttribute {
    name := `temporary_axiom_target_runtime
    descr := "register generated prepared-session target runtime"
    applicationTime := AttributeApplicationTime.afterCompilation
    add := fun declName stx _kind => do
      Attribute.Builtin.ensureNoArgs stx
      let some targetDeclName := parseGeneratedTargetDeclName? declName
        | throwError m!"invalid generated prepared-session target marker {declName}"
      modifyEnv fun env => targetRuntimeExt.addEntry env targetDeclName
    erase := fun _declName => pure ()
  }

public initialize permittedRuntimeAttr : Unit ←
  registerBuiltinAttribute {
    name := `temporary_axiom_permitted_runtime
    descr := "register generated prepared-session permitted-axiom runtime"
    applicationTime := AttributeApplicationTime.afterCompilation
    add := fun declName stx _kind => do
      Attribute.Builtin.ensureNoArgs stx
      let some entry := parseGeneratedPermittedAxiom? declName
        | throwError m!"invalid generated prepared-session permitted marker {declName}"
      modifyEnv fun env => permittedAxiomRuntimeExt.addEntry env entry
    erase := fun _declName => pure ()
  }

public def targetName : CoreM Name := do
  return targetRuntimeExt.getState (← getEnv) |>.getD Name.anonymous

public def targetDeclNameText? : CoreM (Option String) := do
  let frozenTargetName ← targetName
  if frozenTargetName == Name.anonymous then
    return none
  return some (toString frozenTargetName)

public def hasActiveSession : CoreM Bool := do
  return (← targetName) != Name.anonymous

public def permittedAxiomMap : CoreM PermittedAxiomMap := do
  return permittedAxiomRuntimeExt.getState (← getEnv)

public def permittedAxiomFor? (declName : Name) : CoreM (Option PermittedAxiom) := do
  return (← permittedAxiomMap).get? (toString declName)

end TemporaryAxiomTool.PreparedSession
