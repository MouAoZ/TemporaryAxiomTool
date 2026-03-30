import Lean
import TemporaryAxiomTool.ApprovedStatementRegistry

open Lean Elab Command

/--
`@[temporary_axiom]` 用于标记一个“允许临时跳过证明”的 theorem。

工作流分成两步：

1. command macro 在语法层把带标签的 `theorem` 改写成 `axiom`
2. 属性处理器在类型检查后立即核验该声明是否已进入批准注册库，
   并比较当前 elaborated statement 的 hash

因此，后续 section 可以把它当作公理调用，但非法标签会在声明处立刻报错，
而不是等到下游引用时才失败。
-/
syntax (name := Parser.Attr.temporary_axiom) "temporary_axiom" : attr

namespace TemporaryAxiomTool

open TemporaryAxiomTool.ApprovedStatementRegistry

/-- `temporary_axiom` 在 parser、attribute 和 label extension 中共享的统一名字。 -/
def temporaryAxiomAttrName : Name := `temporary_axiom

/--
`temporary_axiom` 是无参数标签。

这里单独包一层，是因为自定义 parser 节点和普通 attribute 语法节点在
语法树中的 kind 不同；二者都要接受，但都不允许额外参数。
-/
private def ensureTemporaryAxiomNoArgs (stx : Syntax) : AttrM Unit := do
  if stx.getKind == ``Parser.Attr.temporary_axiom then
    pure ()
  else
    Attribute.Builtin.ensureNoArgs stx

/--
只从已导入环境中的 Lean 注册表读取批准信息。

这里不会直接访问外部 JSON 数据库；JSON 到 Lean 的同步由离线脚本完成，
本模块只消费生成后的 `approvedStatementMap`。
-/
private def registryEntryFor (declName : Name) : Option ApprovedStatement :=
  approvedStatementMap.find? declName

private def invalidTemporaryAxiomTargetHeader (declName : Name) : MessageData :=
  m!"Invalid @[temporary_axiom] target {.ofConstName declName}"

private def missingApprovedStatementMessage (declName : Name) : MessageData :=
  m!"{invalidTemporaryAxiomTargetHeader declName}\n
The declaration is not present in the approved statement registry.\n
Suggested fixes:\n
- approve the statement with the approved statement registry tool\n
- then refresh the generated registry and re-run this file"

private def statementHashMismatchMessage
    (declName : Name)
    (approvedEntry : ApprovedStatement)
    (actualHash : UInt64) : MessageData :=
  m!"{invalidTemporaryAxiomTargetHeader declName}\n
The approved statement registry entry from {approvedEntry.shardId} does not match the elaborated statement.\n
Expected hash: {(approvedEntry.statementHash.toNat : Nat)}\n
Actual hash:   {(actualHash.toNat : Nat)}\n
Suggested fixes:\n
- if the statement changed intentionally, re-run the registry approve command \n
- otherwise inspect recent edits to the theorem header"

/--
针对生成出的 Lean 注册表校验 `@[temporary_axiom]`。

这个检查发生在声明已经 elaboration 完成之后，因此比较的是最终常量类型的
hash，而不是原始语法。这样可以避免仅从语法表面判断而漏掉隐式参数、
universe 或命名空间解析带来的变化。
-/
private def validateTemporaryAxiomTarget (declName : Name) : AttrM Unit := do
  let constInfo ← getConstInfo declName
  let approvedEntry ← match registryEntryFor declName with
    | some entry => pure entry
    | none =>
        throwError (missingApprovedStatementMessage declName)
  -- 这里使用真正写入环境的常量信息计算 hash，确保比较对象与 Lean 内部语义一致。
  let actualHash := statementHashOfConstInfo constInfo
  if actualHash != approvedEntry.statementHash then
    throwError (statementHashMismatchMessage declName approvedEntry actualHash)

/--
记录所有通过校验并成功进入环境的 `@[temporary_axiom]` 声明。

之所以单独维护一个 `LabelExtension`，是为了让审计命令只依赖环境状态，
而不需要重新扫描源文件或重新解析 attribute 语法。
-/
initialize temporaryAxiomExt : Lean.LabelExtension ← do
  let ext ← Lean.mkLabelExt temporaryAxiomAttrName
  Lean.labelExtensionMapRef.modify fun map => map.insert temporaryAxiomAttrName ext
  pure ext

/--
手动注册 attribute，而不是直接使用 `register_label_attr`。

原因是这里不仅要把声明加入 label 集合，还要先做自定义校验：

- 标签本身不能带参数
- 声明必须已存在于批准注册表
- 当前 elaborated statement hash 必须与批准记录一致

只有全部通过后，声明才会被写入 `temporaryAxiomExt`。
-/
initialize
  registerBuiltinAttribute {
    name := temporaryAxiomAttrName
    descr := "mark a theorem declaration to be compiled as a temporary axiom after approved-statement validation"
    applicationTime := AttributeApplicationTime.afterTypeChecking
    add := fun declName stx kind => do
      ensureTemporaryAxiomNoArgs stx
      -- 在声明完成类型检查时立即核验，因此非法标签会直接定位到本声明。
      validateTemporaryAxiomTarget declName
      temporaryAxiomExt.add declName kind
    erase := fun declName => do
      let state := temporaryAxiomExt.getState (← getEnv)
      -- 删除 attribute 时同步从审计集合中移除，保持环境视图一致。
      modifyEnv fun env => temporaryAxiomExt.modifyState env fun _ => state.erase declName
  }

private def isTemporaryAxiomAttr (attrInstance : Syntax) : Bool :=
  -- 兼容自定义 parser 节点与普通 attribute 语法，避免宏阶段因语法树形态不同而漏判。
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

/--
把带有 `@[temporary_axiom]` 的 theorem 声明头改写成 axiom 声明头。

这里故意只丢弃证明体，不自行重建 binder、universe 或命名空间逻辑，
以最大限度复用 Lean 原生 `axiom` elaborator，降低转换出错的风险。
-/
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
  -- 仅替换声明关键字，保留原始声明头，让 Lean 原生机制继续处理所有细节。
  let declId := decl[1]
  let declSig := decl[2]
  `(command| $(⟨modifiers⟩):declModifiers axiom $(⟨declId⟩):declId $(⟨declSig⟩):declSig)

/-- 返回当前环境中所有 `temporary_axiom` 声明，并按名字排序以稳定审计输出。 -/
def getTemporaryAxioms : CoreM (Array Name) := do
  let decls := temporaryAxiomExt.getState (← getEnv)
  pure <| decls.qsort Name.quickLt

/-- 把待审计的声明名格式化成逐行列表，便于 `#print` 与断言失败时复用。 -/
private def renderDeclList (decls : Array Name) : String :=
  String.intercalate "\n" <| decls.toList.map fun declName => s!"- {declName}"

/-- 打印当前环境中仍然存在的所有 `temporary_axiom`。 -/
elab "#print_temporary_axioms" : command => do
  let decls ← liftCoreM getTemporaryAxioms
  if decls.isEmpty then
    logInfo "No declarations are marked with @[temporary_axiom]."
  else
    logInfo m!"Declarations marked with @[temporary_axiom] ({decls.size}):\n{renderDeclList decls}"

/--
断言当前环境中不再存在任何 `temporary_axiom`。

这个命令适合放在收尾审计或 CI 中，确保最终合并前已经清除全部临时公理。
-/
elab "#assert_no_temporary_axioms" : command => do
  let decls ← liftCoreM getTemporaryAxioms
  unless decls.isEmpty do
    throwError m!"temporary axioms remain in the environment ({decls.size}):\n{renderDeclList decls}"

end TemporaryAxiomTool
