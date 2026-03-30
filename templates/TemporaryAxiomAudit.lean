/-
把这个模板复制到宿主项目根目录，并按需补充业务模块 import。

典型做法：

1. import 你的项目根模块或所有可能引入 `temporary_axiom` 的模块
2. import `TemporaryAxiomTool.TemporaryAxiom`
3. 在 CI 或收尾审计中执行 `lake env lean TemporaryAxiomAudit.lean`
-/
import TemporaryAxiomTool.TemporaryAxiom

#assert_no_temporary_axioms
