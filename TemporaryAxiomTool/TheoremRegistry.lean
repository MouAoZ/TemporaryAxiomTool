module

public import Lean
public meta import Lean
public meta import Lean.Elab.Command
public meta import Lean.Elab.Term
public import TemporaryAxiomTool.StatementHash
public import TemporaryAxiomTool.TheoremRegistry.Types
public meta import TemporaryAxiomTool.StatementHash
public meta import TemporaryAxiomTool.TheoremRegistry.Types

open Lean Parser Elab Command

namespace TemporaryAxiomTool.TheoremRegistry

private meta def moduleNameFor? (env : Environment) (declName : Name) : Option Name :=
  match env.getModuleIdxFor? declName with
  | some moduleIdx => env.header.moduleNames[moduleIdx.toNat]?
  | none => none

private meta def isSupportedProbeConstInfo (constInfo : ConstantInfo) : Bool :=
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
  | some implName => getConstInfo implName
  | none => getConstInfo declName

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
输出单个声明的探测结果，供 Python 侧冻结 theorem-side statement hash。
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
  else if !isSupportedProbeConstInfo constInfo then
    pure ()
  else
    logInfo <|
      (declProbeJson
        declName
        moduleName
        (TemporaryAxiomTool.statementHashOfConstInfo constInfo)).compress

public meta initialize moduleShardRuntimeExt :
    SimplePersistentEnvExtension ModuleShard (NameMap ModuleShard) ←
  registerSimplePersistentEnvExtension {
    name := `temporaryAxiomModuleShardRuntimeExt
    addEntryFn := fun state shard => state.insert shard.hostModule shard
    addImportedFn := fun importedEntries =>
      importedEntries.foldl (init := ({} : NameMap ModuleShard)) fun state entries =>
        entries.foldl (init := state) fun innerState shard =>
          innerState.insert shard.hostModule shard
  }

syntax (name := temporary_axiom_module_shard_runtime_cmd)
  "#register_temporary_axiom_module_shard"
  ppSpace str
  ppSpace str
  ppSpace str
  ppSpace str : command

private meta def parseRegisteredTheoremBatchPayload? (payload : String) : Option RegisteredTheoremBatch := Id.run do
  let mut entries : RegisteredTheoremBatch := #[]
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

private meta def parseModuleShardPayload?
    (hostModuleText : String)
    (targetDeclText : String)
    (targetHashText : String)
    (payloadText : String) : Option ModuleShard := do
  let targetHashNat ← targetHashText.toNat?
  let permittedEntries ← parseRegisteredTheoremBatchPayload? payloadText
  return {
    hostModule := hostModuleText.toName
    targetName := if targetDeclText.isEmpty then Name.anonymous else targetDeclText.toName
    targetHash := targetHashNat.toUInt64
    permitted := insertRegisteredTheoremBatch {} permittedEntries
  }

elab_rules : command
  | `(#register_temporary_axiom_module_shard $hostModule:str $targetDecl:str $targetHash:str $payload:str) => do
      let some shard := parseModuleShardPayload?
        hostModule.getString
        targetDecl.getString
        targetHash.getString
        payload.getString
        | throwError "invalid theorem-registry shard command payload"
      modifyEnv fun env => moduleShardRuntimeExt.addEntry env shard

public meta def shardMap : CoreM (NameMap ModuleShard) := do
  return moduleShardRuntimeExt.getState (← getEnv)

public meta def currentModuleShard? : CoreM (Option ModuleShard) := do
  return (← shardMap).find? (← getEnv).mainModule

public meta def currentModuleTracked : CoreM Bool := do
  return (← currentModuleShard?).isSome

public meta def targetName : CoreM Name := do
  return (← currentModuleShard?).map (·.targetName) |>.getD Name.anonymous

public meta def targetHash : CoreM UInt64 := do
  return (← currentModuleShard?).map (·.targetHash) |>.getD 0

public meta def hasActiveSession : CoreM Bool := do
  return (← targetName) != Name.anonymous

public meta def permittedTheoremFor? (declName : Name) : CoreM (Option RegisteredTheorem) := do
  return (← currentModuleShard?).bind (·.permittedFor? declName)

end TemporaryAxiomTool.TheoremRegistry
