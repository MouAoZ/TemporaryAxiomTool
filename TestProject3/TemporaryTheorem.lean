import Lean

open Lean Elab Command

register_label_attr temporary_axiom

namespace TemporaryTheorem

private def isTemporaryAxiomAttr (attrInstance : Syntax) : Bool :=
  -- 检查传入节点是否是属性实例
  if attrInstance.getKind != ``Parser.Term.attrInstance then
    false
  else
    let attr := attrInstance[1]
    attr.getKind == ``Parser.Attr.temporary_axiom ||
    -- Fallback到Parser.Attr.simple以支持旧语法
      (attr.getKind == ``Parser.Attr.simple &&
        attr[0].getId.eraseMacroScopes == `temporary_axiom &&
        attr[1].isNone &&
        attr[2].isNone)

private def hasTemporaryAxiomAttr (modifiers : Syntax) : Bool :=
  --查找@[temporary_axiom]属性
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
  -- decl的语法树节点stx由修饰符(stx[0])和声明本体(stx[1])组成
  let modifiers := stx[0]
  let decl := stx[1]
  -- 只拦截 theorem 声明，并且必须带有@[temporary_axiom]属性
  if decl.getKind != ``Parser.Command.theorem then
    Macro.throwUnsupported
  if !hasTemporaryAxiomAttr modifiers then
    Macro.throwUnsupported
  -- theorem的语法树节点decl由关键字"theorem"(decl[0]), 标识符(decl[1], 名字与宇宙参数), 签名(decl[2], 参数和返回类型)和赋值部分(decl[3], 证明体)组成.
  -- 这里抛弃赋值部分, 对剩余部分执行强制类型转换, 并重新构造一个axiom声明的语法树节点返回.
  let declId := decl[1]
  let declSig := decl[2]
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
