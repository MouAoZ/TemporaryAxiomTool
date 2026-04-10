module

public import Lean
public import TemporaryAxiomTool.StatementHash
public import TemporaryAxiomTool.TheoremRegistry
public meta import Lean
public meta import Lean.Elab.Command
public meta import Lean.Elab.Term
public meta import TemporaryAxiomTool.StatementHash
public meta import TemporaryAxiomTool.TheoremRegistry

open Lean Elab Command

syntax (name := Parser.Attr.temporary_axiom) "temporary_axiom" : attr

namespace TemporaryAxiomTool

open TemporaryAxiomTool.TheoremRegistry

private meta def userVisibleDeclName (declName : Name) : Name :=
  match privateToUserName? declName.eraseMacroScopes with
  | some userName => userName
  | none => declName.eraseMacroScopes

private meta def ensureTemporaryAxiomNoArgs (stx : Syntax) : AttrM Unit := do
  if stx.getKind == ``Parser.Attr.temporary_axiom then
    pure ()
  else
    Attribute.Builtin.ensureNoArgs stx

private meta def invalidTemporaryAxiomTargetHeader (declName : Name) : MessageData :=
  m!"Invalid @[temporary_axiom] target {.ofConstName declName}"

private meta def noActiveSessionMessage (declName : Name) : MessageData :=
  m!"{invalidTemporaryAxiomTargetHeader declName}\n
No active theorem-registry session is loaded.\n
Suggested fixes:\n
- run the session `prepare` command before using `@[temporary_axiom]`\n
- or remove the attribute from this declaration"

private meta def targetTheoremTaggedMessage (declName : Name) : MessageData :=
  m!"{invalidTemporaryAxiomTargetHeader declName}\n
The frozen target theorem itself must not carry `@[temporary_axiom]`.\n
Suggested fixes:\n
- remove the attribute from the target theorem\n
- or regenerate the prepared session if the target changed"

private meta def declarationNotPermittedMessage (declName : Name) : MessageData :=
  m!"{invalidTemporaryAxiomTargetHeader declName}\n
The declaration is not listed in the current session's permitted theorems.\n
Suggested fixes:\n
- remove the attribute from this declaration\n
- or regenerate the prepared session so the permitted set is refreshed"

private meta def statementHashMismatchMessage
    (declName : Name)
    (expectedHash : UInt64)
    (actualHash : UInt64) : MessageData :=
  m!"Temporary axiom statement hash mismatch for {.ofConstName declName}\n
Expected hash: {(expectedHash.toNat : Nat)}\n
Actual hash:   {(actualHash.toNat : Nat)}\n
Suggested fixes:\n
- if the statement changed intentionally, re-run the session `prepare` command\n
- otherwise inspect recent edits to the theorem header"

private meta def targetHashMismatchMessage
    (declName : Name)
    (expectedHash : UInt64)
    (actualHash : UInt64) : MessageData :=
  m!"Session target statement hash mismatch for {.ofConstName declName}\n
Expected hash: {(expectedHash.toNat : Nat)}\n
Actual hash:   {(actualHash.toNat : Nat)}\n
Suggested fixes:\n
- if the statement changed intentionally, re-run the session `prepare` command\n
- otherwise inspect recent edits to the target theorem header"

private meta def isTemporaryAxiomAttr (attrInstance : Syntax) : Bool :=
  if attrInstance.getKind != ``Parser.Term.attrInstance then
    false
  else
    let attr := attrInstance[1]
    attr.getKind == ``Parser.Attr.temporary_axiom ||
      (attr.getKind == ``Parser.Attr.simple &&
        attr[0].getId.eraseMacroScopes == `temporary_axiom &&
        attr[1].isNone &&
        attr[2].isNone)

private meta def hasTemporaryAxiomAttr (modifiers : Syntax) : Bool :=
  if modifiers.getKind != ``Parser.Command.declModifiers then
    false
  else
    let attrsOpt := modifiers[1]
    if attrsOpt.isNone then
      false
    else
      let attrs := attrsOpt[0][1].getSepArgs
      attrs.any isTemporaryAxiomAttr

private meta def buildAxiomizedDeclaration
    (modifiers : TSyntax ``Parser.Command.declModifiers)
    (declId : TSyntax ``Parser.Command.declId)
    (declSig : TSyntax ``Parser.Command.declSig) : MacroM Syntax :=
  `(command| $modifiers:declModifiers axiom $declId:declId $declSig:declSig)

private structure ResolvedTheoremDecl where
  modifiers : Modifiers
  decl : Syntax
  declId : TSyntax ``Parser.Command.declId
  declSig : TSyntax ``Parser.Command.declSig
  declName : Name
  userDeclName : Name

private meta def resolveTheoremDecl (stx : Syntax) : CommandElabM ResolvedTheoremDecl := do
  let modifiersStx : TSyntax ``Parser.Command.declModifiers := ⟨stx[0]⟩
  let decl := stx[1]
  let declId : TSyntax ``Parser.Command.declId := ⟨decl[1]⟩
  let declSig : TSyntax ``Parser.Command.declSig := ⟨decl[2]⟩
  let modifiers ← elabModifiers modifiersStx
  let declName ← runTermElabM fun _ => do
    let scopeLevelNames ← Term.getLevelNames
    let ⟨_, declName, _, _⟩ ←
      Term.expandDeclId (← getCurrNamespace) scopeLevelNames declId modifiers
    pure declName
  return {
    modifiers := modifiers
    decl := decl
    declId := declId
    declSig := declSig
    declName := declName
    userDeclName := userVisibleDeclName declName
  }

private meta def elaborateTheoremHeaderHash
    (resolved : ResolvedTheoremDecl) : CommandElabM UInt64 := do
  let decl := resolved.decl
  let modifiers := resolved.modifiers
  let declId := resolved.declId
  let declSig := resolved.declSig
  let (binders, typeStx) := expandDeclSig declSig.raw
  runTermElabM fun vars => do
    let scopeLevelNames ← Term.getLevelNames
    let ⟨shortName, declName, allUserLevelNames, _⟩ ←
      Term.expandDeclId (← getCurrNamespace) scopeLevelNames declId modifiers
    addDeclarationRangesForBuiltin declName modifiers.stx decl
    Term.withAutoBoundImplicitForbiddenPred (fun n => shortName == n) do
      Term.withDeclName declName <|
        Term.withLevelNames allUserLevelNames <|
        Term.elabBinders binders.getArgs fun xs => do
          let type ← Term.elabType typeStx
          Term.synthesizeSyntheticMVarsNoPostponing
          let xs ← Term.addAutoBoundImplicits xs (declId.raw.getTailPos? (canonicalOnly := true))
          let type ← instantiateMVars type
          let type ← Meta.mkForallFVars xs type
          let type ← Meta.mkForallFVars vars type (usedOnly := true)
          let type ← Term.levelMVarToParam type
          let usedParams := collectLevelParams {} type |>.params
          let levelParams ← match sortDeclLevelParams scopeLevelNames allUserLevelNames usedParams with
            | Except.ok params => pure params
            | Except.error msg => throwErrorAt decl msg
          let type ← instantiateMVars type
          return TemporaryAxiomTool.statementHash levelParams type

private meta def validateTemporaryAxiomAttrTarget (declName : Name) : AttrM Unit := do
  let runtimeDeclName := userVisibleDeclName declName
  let frozenTargetName ← targetName
  if frozenTargetName == Name.anonymous then
    throwError (noActiveSessionMessage runtimeDeclName)
  if runtimeDeclName == frozenTargetName then
    throwError (targetTheoremTaggedMessage runtimeDeclName)
  let permittedEntry ← match (← permittedTheoremFor? runtimeDeclName) with
    | some entry => pure entry
    | none => throwError (declarationNotPermittedMessage runtimeDeclName)
  let constInfo ← getConstInfo declName
  let actualHash := TemporaryAxiomTool.statementHashOfConstInfo constInfo
  if actualHash != permittedEntry.statementHash then
    throwError (statementHashMismatchMessage runtimeDeclName permittedEntry.statementHash actualHash)

public meta initialize temporaryAxiomAttrInitialized : Unit ← do
  let attrName : Name := `temporary_axiom
  registerBuiltinAttribute {
    name := attrName
    descr := "mark a theorem declaration to be compiled as a temporary axiom after session validation"
    applicationTime := AttributeApplicationTime.afterTypeChecking
    add := fun declName stx _kind => do
      ensureTemporaryAxiomNoArgs stx
      validateTemporaryAxiomAttrTarget declName
    erase := fun _declName => pure ()
  }

@[command_elab declaration]
public meta def elabTrackedTheoremDeclaration : CommandElab := fun stx => do
  let decl := stx[1]
  if decl.getKind != ``Parser.Command.theorem then
    throwUnsupportedSyntax
  if !(← liftCoreM hasActiveSession) then
    throwUnsupportedSyntax
  let resolved ← resolveTheoremDecl stx
  let explicitTemporaryAxiom := hasTemporaryAxiomAttr stx[0]
  let frozenTargetName ← liftCoreM targetName
  let permittedEntry? ← liftCoreM <| permittedTheoremFor? resolved.userDeclName
  let isTarget := resolved.userDeclName == frozenTargetName
  if !isTarget && permittedEntry?.isNone then
    if explicitTemporaryAxiom then
      throwError (declarationNotPermittedMessage resolved.userDeclName)
    throwUnsupportedSyntax
  if isTarget && explicitTemporaryAxiom then
    throwError (targetTheoremTaggedMessage resolved.userDeclName)
  let actualHash ← elaborateTheoremHeaderHash resolved
  if isTarget then
    let expectedHash ← liftCoreM targetHash
    if actualHash != expectedHash then
      throwError (targetHashMismatchMessage resolved.userDeclName expectedHash actualHash)
    Command.elabDeclaration stx
  else
    let some permittedEntry := permittedEntry?
      | throwUnsupportedSyntax
    if actualHash != permittedEntry.statementHash then
      throwError (statementHashMismatchMessage resolved.userDeclName permittedEntry.statementHash actualHash)
    let axiomStx ← liftMacroM <|
      buildAxiomizedDeclaration ⟨stx[0]⟩ resolved.declId resolved.declSig
    Command.elabDeclaration axiomStx

end TemporaryAxiomTool
