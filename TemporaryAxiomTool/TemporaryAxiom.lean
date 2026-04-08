module

public import Lean
public import TemporaryAxiomTool.PreparedSession
public meta import Lean
public meta import Lean.Elab.Command
public meta import TemporaryAxiomTool.PreparedSession

open Lean Elab Command

/- 
`@[temporary_axiom]` 标记一个“允许临时跳过证明”的 theorem。

这里显式保留 attribute syntax，并在 import 阶段完成 attribute 注册。
theorem 头部由 `macro_rules` 改写成 axiom，随后 attribute 在类型检查后立即
核验当前 prepared session 与 statement hash，让错误直接落在声明处。
-/
syntax (name := Parser.Attr.temporary_axiom) "temporary_axiom" : attr

namespace TemporaryAxiomTool

open TemporaryAxiomTool.PreparedSession

/--
`temporary_axiom` 是无参数标签。

显式保留 attribute syntax，有利于下游模块在导入后稳定识别该 attribute。
-/
private meta def ensureTemporaryAxiomNoArgs (stx : Syntax) : AttrM Unit := do
  if stx.getKind == ``Parser.Attr.temporary_axiom then
    pure ()
  else
    Attribute.Builtin.ensureNoArgs stx

/--
只从已导入环境中的当前 session runtime 读取 permitted axioms。

JSON 到 Lean 的同步由离线脚本完成；这里始终只认当前环境里注册过的 target /
permitted axioms。
-/
private meta def permittedEntryFor (declName : Name) : AttrM (Option PermittedAxiom) := do
  permittedAxiomFor? declName

-- 报错正文保持英文，便于直接出现在 Lean/CI 输出中；这里不拼双语长消息。
private meta def invalidTemporaryAxiomTargetHeader (declName : Name) : MessageData :=
  m!"Invalid @[temporary_axiom] target {.ofConstName declName}"

private meta def noActivePreparedSessionMessage (declName : Name) : MessageData :=
  m!"{invalidTemporaryAxiomTargetHeader declName}\n
No active prepared session is loaded.\n
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
The declaration is not listed in the current session's permitted axioms.\n
Suggested fixes:\n
- remove the attribute from this declaration\n
- or regenerate the prepared session so the permitted set is refreshed"

private meta def statementHashMismatchMessage
    (declName : Name)
    (permittedEntry : PermittedAxiom)
    (actualHash : UInt64) : MessageData :=
  m!"{invalidTemporaryAxiomTargetHeader declName}\n
The frozen prepared-session entry does not match the elaborated statement.\n
Expected hash: {(permittedEntry.statementHash.toNat : Nat)}\n
Actual hash:   {(actualHash.toNat : Nat)}\n
Suggested fixes:\n
- if the statement changed intentionally, re-run the session `prepare` command\n
- otherwise inspect recent edits to the theorem header"

/--
针对当前 prepared session runtime 校验 `@[temporary_axiom]`。

比较发生在 elaboration 之后，因此看到的是最终常量类型，而不是原始语法。
这样能拦住隐式参数、universe 或命名空间解析导致的真实陈述漂移。
-/
private meta def validateTemporaryAxiomTarget (declName : Name) : AttrM Unit := do
  let some frozenTargetText ← targetDeclNameText? | do
    throwError (noActivePreparedSessionMessage declName)
  if toString declName == frozenTargetText then
    throwError (targetTheoremTaggedMessage declName)
  let constInfo ← getConstInfo declName
  let permittedEntry ← match (← permittedEntryFor declName) with
    | some entry => pure entry
    | none =>
        throwError m!"{declarationNotPermittedMessage declName}\n
Frozen target: {frozenTargetText}"
  -- 这里使用真正写入环境的常量信息计算 hash，确保比较对象与 Lean 内部语义一致。
  let actualHash := TemporaryAxiomTool.statementHashOfConstInfo constInfo
  if actualHash != permittedEntry.statementHash then
    throwError (statementHashMismatchMessage declName permittedEntry actualHash)

/--
手动注册 attribute，而不是直接使用 `register_label_attr`。

这里要先做无参数检查、target/permitted/hash 检查，因此不能直接用
`register_label_attr`。

使用 `public meta initialize`，确保导入方在 elaboration 阶段就能看到这条注册动作。
-/
public meta initialize temporaryAxiomAttrInitialized : Unit ← do
  let attrName : Name := `temporary_axiom
  registerBuiltinAttribute {
    name := attrName
    descr := "mark a theorem declaration to be compiled as a temporary axiom after prepared-session validation"
    applicationTime := AttributeApplicationTime.afterTypeChecking
    add := fun declName stx _kind => do
      ensureTemporaryAxiomNoArgs stx
      -- 在声明完成类型检查时立即核验，因此非法标签会直接定位到本声明。
      validateTemporaryAxiomTarget declName
    erase := fun _declName => pure ()
  }

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

/--
把带有 `@[temporary_axiom]` 的 theorem 声明头改写成 axiom 声明头。

这里只丢弃证明体，不重建 binder 或 universe 细节，尽量复用 Lean 自身的
`axiom` elaborator，减少宏层面的语义偏差。这里使用 `macro_rules`，避免继续
依赖较底层的 `@[macro Parser.Command.declaration]` 挂接方式。
-/
macro_rules (kind := Lean.Parser.Command.declaration)
  | `($modifiers:declModifiers theorem $declId:declId $declSig:declSig $_:declVal) => do
      if !hasTemporaryAxiomAttr modifiers then
        Macro.throwUnsupported
      -- 仅替换声明关键字，保留原始声明头，让 Lean 原生机制继续处理所有细节。
      `(command| $modifiers:declModifiers axiom $declId:declId $declSig:declSig)

end TemporaryAxiomTool
