import Lean

open Lean Elab Command

register_label_attr temporary_axiom

namespace TemporaryTheorem

private def isTemporaryAxiomAttr (attrInstance : Syntax) : Bool :=
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
  let declId := decl[1]
  let declSig := decl[2]
  -- Preserve the original declaration header verbatim and let Lean's built-in
  -- `axiom` elaborator handle namespaces, binders, implicits, and universes.
  `(command| $(⟨modifiers⟩):declModifiers axiom $(⟨declId⟩):declId $(⟨declSig⟩):declSig)

def getTemporaryAxioms : CoreM (Array Name) := do
  let decls ← Lean.labelled `temporary_axiom
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

end TemporaryTheorem
