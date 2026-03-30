import Lean
import TestProject3.ApprovedStatementRegistry

open Lean Elab Command

/--
`@[temporary_axiom]` marks a theorem whose statement has been pre-approved in the
approved statement registry. The theorem body is discarded by a command macro
and the declaration is compiled as an `axiom`.

The attribute is still checked after type checking so an invalid tag fails at the
declaration site immediately.
-/
syntax (name := Parser.Attr.temporary_axiom) "temporary_axiom" : attr

namespace TemporaryAxiom

open TestProject3.ApprovedStatementRegistry

def temporaryAxiomAttrName : Name := `temporary_axiom

private def ensureTemporaryAxiomNoArgs (stx : Syntax) : AttrM Unit := do
  if stx.getKind == ``Parser.Attr.temporary_axiom then
    pure ()
  else
    Attribute.Builtin.ensureNoArgs stx

private def registryEntryFor (declName : Name) : Option ApprovedStatement :=
  approvedStatementMap.find? declName

/--
Validate `@[temporary_axiom]` against the generated Lean registry.

This runs after the rewritten declaration has been elaborated, so the hash is
computed from the final Lean type instead of from raw syntax.
-/
private def validateTemporaryAxiomTarget (declName : Name) : AttrM Unit := do
  let constInfo ← getConstInfo declName
  let approvedEntry ← match registryEntryFor declName with
    | some entry => pure entry
    | none =>
        throwError m!"Invalid @[temporary_axiom] target {.ofConstName declName}: the declaration is not present in the approved statement registry. \
Approve the statement before adding the tag by running the approved statement registry tool."
  let actualHash := statementHashOfConstInfo constInfo
  if actualHash != approvedEntry.statementHash then
    throwError m!"Invalid @[temporary_axiom] target {.ofConstName declName}: the approved statement registry entry from \
{approvedEntry.shardId} does not match the elaborated statement. Expected hash {(approvedEntry.statementHash.toNat : Nat)}, \
but the declaration elaborated to hash {(actualHash.toNat : Nat)}."

initialize temporaryAxiomExt : Lean.LabelExtension ← do
  let ext ← Lean.mkLabelExt temporaryAxiomAttrName
  Lean.labelExtensionMapRef.modify fun map => map.insert temporaryAxiomAttrName ext
  pure ext

/--
Register the attribute manually instead of using `register_label_attr`, because
we need custom validation before the declaration is accepted into the label set.
-/
initialize
  registerBuiltinAttribute {
    name := temporaryAxiomAttrName
    descr := "mark a theorem declaration to be compiled as a temporary axiom after approved-statement validation"
    applicationTime := AttributeApplicationTime.afterTypeChecking
    add := fun declName stx kind => do
      ensureTemporaryAxiomNoArgs stx
      -- The registry check runs while the tagged declaration is being elaborated,
      -- so invalid tags fail at the declaration site instead of on downstream use.
      validateTemporaryAxiomTarget declName
      temporaryAxiomExt.add declName kind
    erase := fun declName => do
      let state := temporaryAxiomExt.getState (← getEnv)
      modifyEnv fun env => temporaryAxiomExt.modifyState env fun _ => state.erase declName
  }

private def isTemporaryAxiomAttr (attrInstance : Syntax) : Bool :=
  -- Accept both the custom parser node and the simple attribute form so older
  -- syntax trees still match during macro expansion.
  if attrInstance.getKind != ``Parser.Term.attrInstance then
    false
  else
    let attr := attrInstance[1]
    attr.getKind == ``Parser.Attr.temporary_axiom ||
      (attr.getKind == ``Parser.Attr.simple &&
        attr[0].getId.eraseMacroScopes == `temporary_axiom &&
        attr[1].isNone &&
        attr[2].isNone)

private def hasTemporaryAxiomAttr (modifiers : Syntax) : Bool :=
  if modifiers.getKind != ``Parser.Command.declModifiers then
    false
  else
    let attrsOpt := modifiers[1]
    if attrsOpt.isNone then
      false
    else
      let attrs := attrsOpt[0][1].getSepArgs
      attrs.any isTemporaryAxiomAttr

@[macro Lean.Parser.Command.declaration]
def expandTemporaryAxiomTheorem : Macro := fun stx => do
  if stx.getKind != ``Parser.Command.declaration then
    Macro.throwUnsupported
  let modifiers := stx[0]
  let decl := stx[1]
  if decl.getKind != ``Parser.Command.theorem then
    Macro.throwUnsupported
  if !hasTemporaryAxiomAttr modifiers then
    Macro.throwUnsupported
  -- Keep the original declaration header and only discard the proof body.
  -- Lean's native `axiom` elaborator then handles namespaces, binders,
  -- implicit arguments, and other declaration details exactly as usual.
  let declId := decl[1]
  let declSig := decl[2]
  `(command| $(⟨modifiers⟩):declModifiers axiom $(⟨declId⟩):declId $(⟨declSig⟩):declSig)

def getTemporaryAxioms : CoreM (Array Name) := do
  let decls := temporaryAxiomExt.getState (← getEnv)
  pure <| decls.qsort Name.quickLt

private def renderDeclList (decls : Array Name) : String :=
  String.intercalate "\n" <| decls.toList.map fun declName => s!"- {declName}"

elab "#print_temporary_axioms" : command => do
  let decls ← liftCoreM getTemporaryAxioms
  if decls.isEmpty then
    logInfo "No declarations are marked with @[temporary_axiom]."
  else
    logInfo m!"Declarations marked with @[temporary_axiom] ({decls.size}):\n{renderDeclList decls}"

elab "#assert_no_temporary_axioms" : command => do
  let decls ← liftCoreM getTemporaryAxioms
  unless decls.isEmpty do
    throwError "temporary axioms remain in the environment:\n{renderDeclList decls}"

end TemporaryAxiom
