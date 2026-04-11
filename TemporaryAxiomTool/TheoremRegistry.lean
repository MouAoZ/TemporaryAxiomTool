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

private meta def userVisibleDeclName (declName : Name) : Name :=
  match privateToUserName? declName.eraseMacroScopes with
  | some userName => userName
  | none => declName.eraseMacroScopes

public meta initialize probeEnvironmentCache :
    IO.Ref (Option (Array Name × Environment)) ←
  IO.mkRef none

private meta def probeEnvironment : CommandElabM Environment := do
  let env ← getEnv
  let imports := env.header.moduleNames.filter fun moduleName => moduleName != env.mainModule
  let cached ← liftIO <| probeEnvironmentCache.get
  if let some (cachedImports, cachedEnv) := cached then
    if cachedImports == imports then
      return cachedEnv
  let importedEnv ← liftIO <| importModules (imports.map fun moduleName => { module := moduleName }) {}
  liftIO <| probeEnvironmentCache.set (some (imports, importedEnv))
  return importedEnv

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

private meta def effectiveConstInfoFor? (env : Environment) (declName : Name) : Option ConstantInfo :=
  match implementationNameFor? env declName with
  | some implName => env.find? implName
  | none => env.find? declName

private meta def lookupProbeConstName? (env : Environment) (declName : Name) : Option Name :=
  let visibleName := userVisibleDeclName declName
  if env.find? declName |>.isSome then
    some declName
  else if env.find? visibleName |>.isSome then
    some visibleName
  else
    (implementationNameFor? env declName) <|> (implementationNameFor? env visibleName)

private meta def declProbeJson
    (requestedName : Name)
    (declName : Name)
    (moduleName : Name)
    (statementHash : UInt64) : Json :=
  Json.mkObj [
    ("requested_name", Json.str <| toString requestedName),
    ("decl_name", Json.str <| toString declName),
    ("module", Json.str <| toString moduleName),
    ("statement_hash", Json.str <| toString statementHash.toNat)
  ]

/--
输出单个声明的探测结果，供 Python 侧冻结 theorem-side statement hash。
-/
private meta def logDeclProbeFor (requestedName : Name) : CommandElabM Unit := do
  let env ← probeEnvironment
  let some lookupName := lookupProbeConstName? env requestedName
    | pure ()
  let visibleName := userVisibleDeclName lookupName
  let some constInfo := effectiveConstInfoFor? env visibleName
    | pure ()
  let moduleName := ((moduleNameFor? env visibleName) <|> (moduleNameFor? env lookupName)).getD Name.anonymous
  if moduleName == Name.anonymous then
    pure ()
  else if !isSupportedProbeConstInfo constInfo then
    pure ()
  else
    liftIO <| IO.println <|
      (declProbeJson
        requestedName
        visibleName
        moduleName
        (TemporaryAxiomTool.statementHashOfConstInfo constInfo)).compress

elab "#print_temporary_axiom_decl_probe " quotedName:term : command => do
  let declName ← liftTermElabM do
    unsafe Term.evalTerm Name (mkConst ``Name) quotedName
  logDeclProbeFor declName

elab "#print_temporary_axiom_decl_probe_text " declName:str : command => do
  logDeclProbeFor declName.getString.toName

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
    (modeText : String)
    (targetDeclText : String)
    (targetHashText : String)
    (payloadText : String) : Option ModuleShard := do
  let targetHashNat ← targetHashText.toNat?
  let permittedEntries ← parseRegisteredTheoremBatchPayload? payloadText
  let mode ←
    match modeText with
    | "inactive" => some ModuleShardMode.inactive
    | "collect" => some ModuleShardMode.collect
    | "active" => some ModuleShardMode.active
    | _ => none
  return {
    hostModule := hostModuleText.toName
    mode := mode
    targetName := if targetDeclText.isEmpty then Name.anonymous else targetDeclText.toName
    targetHash := targetHashNat.toUInt64
    permitted := insertRegisteredTheoremBatch {} permittedEntries
  }

elab_rules : command
  | `(#register_temporary_axiom_module_shard
      $hostModule:str
      $mode:str
      $targetDecl:str
      $targetHash:str
      $payload:str) => do
      let some shard := parseModuleShardPayload?
        hostModule.getString
        mode.getString
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

public meta def currentModuleMode : CoreM ModuleShardMode := do
  return (← currentModuleShard?).map (·.mode) |>.getD .inactive

public meta def targetName : CoreM Name := do
  return (← currentModuleShard?).map (·.targetName) |>.getD Name.anonymous

public meta def targetHash : CoreM UInt64 := do
  return (← currentModuleShard?).map (·.targetHash) |>.getD 0

public meta def hasActiveSession : CoreM Bool := do
  return (← currentModuleMode) == .active

public meta def inCollectMode : CoreM Bool := do
  return (← currentModuleMode) == .collect

public meta def permittedTheoremFor? (declName : Name) : CoreM (Option RegisteredTheorem) := do
  return (← currentModuleShard?).bind (·.permittedFor? declName)

end TemporaryAxiomTool.TheoremRegistry
