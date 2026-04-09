module

public import Lean
public meta import Lean
public meta import Lean.Elab.Command
public meta import Lean.Elab.Term
public import TemporaryAxiomTool.StatementHash
public import TemporaryAxiomTool.PreparedSession.Types
public meta import TemporaryAxiomTool.PreparedSession.Types
public meta import TemporaryAxiomTool.StatementHash

open Lean Parser Elab Command

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

public initialize targetRuntimeExt : SimplePersistentEnvExtension Name (Option Name) ←
  registerSimplePersistentEnvExtension {
    name := `temporaryAxiomTargetRuntimeExt
    addEntryFn := fun _ declName => some declName
    addImportedFn := fun importedEntries =>
      importedEntries.foldl (init := none) fun state entries =>
        entries.foldl (init := state) fun _ declName => some declName
  }

private def insertPermittedAxiomBatch
    (state : PermittedAxiomMap)
    (entries : PermittedAxiomBatch) : PermittedAxiomMap :=
  insertPermittedAxioms state entries

private def insertImportedPermittedAxiomBatches
    (importedEntries : Array (Array PermittedAxiomBatch)) : PermittedAxiomMap :=
  importedEntries.foldl (init := {}) fun state entries =>
    entries.foldl (init := state) insertPermittedAxiomBatch

private def permittedAxiomBatchHasFreshEntry
    (state : PermittedAxiomMap)
    (entries : PermittedAxiomBatch) : Bool :=
  entries.any fun entry => !state.contains entry.name

private def replayPermittedAxiomBatches
    (newEntries : List PermittedAxiomBatch)
    (_newState : PermittedAxiomMap)
    (state : PermittedAxiomMap) : List PermittedAxiomBatch × PermittedAxiomMap :=
  let newEntries := newEntries.filter (permittedAxiomBatchHasFreshEntry state)
  let state := newEntries.foldl (init := state) insertPermittedAxiomBatch
  (newEntries, state)

public initialize permittedAxiomRuntimeExt : SimplePersistentEnvExtension PermittedAxiomBatch PermittedAxiomMap ←
  registerSimplePersistentEnvExtension {
    name := `temporaryAxiomPermittedRuntimeExt
    addEntryFn := insertPermittedAxiomBatch
    addImportedFn := insertImportedPermittedAxiomBatches
    replay? := some replayPermittedAxiomBatches
  }

syntax (name := Parser.Attr.temporary_axiom_target_runtime)
  "temporary_axiom_target_runtime" ppSpace str : attr

syntax (name := Parser.Attr.temporary_axiom_permitted_runtime_batch)
  "temporary_axiom_permitted_runtime_batch" ppSpace str : attr

private def parseTargetRuntimeAttr? (stx : Syntax) : Option Name :=
  if stx.getKind == ``Parser.Attr.temporary_axiom_target_runtime then
    stx[1].isStrLit?.map String.toName
  else
    none

private def parsePermittedRuntimeBatchAttr? (stx : Syntax) : Option String :=
  if stx.getKind == ``Parser.Attr.temporary_axiom_permitted_runtime_batch then
    stx[1].isStrLit?
  else
    none

private def parsePermittedRuntimeBatchPayload? (payload : String) : Option PermittedAxiomBatch := Id.run do
  let mut entries : PermittedAxiomBatch := #[]
  if payload.isEmpty then
    return some entries
  for line in payload.splitOn "\n" do
    if line.isEmpty then
      return none
    match line.splitOn "\t" with
    | [declNameText, statementHashText] =>
        let some statementHashNat := statementHashText.toNat?
          | return none
        entries := entries.push {
          name := declNameText.toName
          statementHash := statementHashNat.toUInt64
        }
    | _ =>
        return none
  return some entries

public initialize targetRuntimeAttr : Unit ←
  registerBuiltinAttribute {
    name := `temporary_axiom_target_runtime
    descr := "register generated prepared-session target runtime"
    applicationTime := AttributeApplicationTime.afterTypeChecking
    add := fun _declName stx _kind => do
      let some targetDeclName := parseTargetRuntimeAttr? stx
        | throwError m!"invalid prepared-session target runtime attribute syntax{indentD stx}"
      modifyEnv fun env => targetRuntimeExt.addEntry env targetDeclName
    erase := fun _declName => pure ()
  }

public initialize permittedRuntimeBatchAttr : Unit ←
  registerBuiltinAttribute {
    name := `temporary_axiom_permitted_runtime_batch
    descr := "register generated prepared-session permitted-axiom runtime batch"
    applicationTime := AttributeApplicationTime.afterTypeChecking
    add := fun _declName stx _kind => do
      let some payload := parsePermittedRuntimeBatchAttr? stx
        | throwError m!"invalid prepared-session permitted runtime batch attribute syntax{indentD stx}"
      let some entries := parsePermittedRuntimeBatchPayload? payload
        | throwError m!"invalid prepared-session permitted runtime batch payload"
      modifyEnv fun env => permittedAxiomRuntimeExt.addEntry env entries
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
  return (← permittedAxiomMap).find? declName

end TemporaryAxiomTool.PreparedSession
