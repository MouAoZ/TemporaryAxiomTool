module

public meta import Lean

namespace TemporaryAxiomTool

/--
记录所有通过校验并进入环境的 `@[temporary_axiom]` 声明。

把 extension 拆到独立 meta 模块里，避免与主入口文件的普通 helper 混在一起。
-/
public meta initialize temporaryAxiomExt : Lean.LabelExtension ← do
  let attrName : Lean.Name := `temporary_axiom
  let ext ← Lean.mkLabelExt attrName
  Lean.labelExtensionMapRef.modify fun map => map.insert attrName ext
  pure ext

/-- 返回当前环境中的全部 `temporary_axiom`，并按名字排序以稳定审计输出。 -/
public meta def getTemporaryAxioms : Lean.CoreM (Array Lean.Name) := do
  let decls := temporaryAxiomExt.getState (← Lean.getEnv)
  pure <| decls.qsort Lean.Name.quickLt

end TemporaryAxiomTool
