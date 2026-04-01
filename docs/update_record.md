# 更新记录

这份文档只记录“版本更新后下游项目需要检查什么改变了”。
它同时替代旧的 `module_system_refactor_report.md`。

如果你需要：

- 升级步骤与测试清单：看 [downstream_upgrade_note.md](./downstream_upgrade_note.md)
- 完整技术规格与命令参数：看 [temporary_axiom.md](./temporary_axiom.md)
- 数据库 schema：看 [../approved_statement_registry_db/README.md](../approved_statement_registry_db/README.md)

## 当前版本需要同步的外部引用

### 文件结构

下游项目应同步整个工具集合，而不是只替换单个 Lean 文件：

- `TemporaryAxiomTool.lean`
- `TemporaryAxiomTool/`
- `scripts/manage_approved_statement_registry.py`
- `scripts/registry_tool/`
- `docs/update_record.md`
- `docs/temporary_axiom.md`
- `approved_statement_registry_db/README.md`

新增且需要一并同步的 Lean 文件：

- [../TemporaryAxiomTool/ApprovedStatementRegistry/Hash.lean](../TemporaryAxiomTool/ApprovedStatementRegistry/Hash.lean)
- [../TemporaryAxiomTool/TemporaryAxiom/Runtime.lean](../TemporaryAxiomTool/TemporaryAxiom/Runtime.lean)

### CI 与命令入口

旧引用应删除：

- `./scripts/run_approved_statement_registry_audit.sh`

当前统一入口：

- `python3 scripts/manage_approved_statement_registry.py audit`
- `python3 scripts/manage_approved_statement_registry.py audit-temporary-axioms --module <YourProject>`

## 本轮关键变更

### Lean 侧结构

- `statement hash` 计算拆到 [../TemporaryAxiomTool/ApprovedStatementRegistry/Hash.lean](../TemporaryAxiomTool/ApprovedStatementRegistry/Hash.lean)
- `temporary_axiom` 的 extension 与环境查询拆到 [../TemporaryAxiomTool/TemporaryAxiom/Runtime.lean](../TemporaryAxiomTool/TemporaryAxiom/Runtime.lean)
- [../TemporaryAxiomTool/TemporaryAxiom.lean](../TemporaryAxiomTool/TemporaryAxiom.lean) 只保留 attribute、theorem 改写与审计命令

这样做是为了让 module system 下的 ordinary 逻辑与 metaprogramming 入口分离，避免 phase 访问错误。

### Lean 侧实现口径

- theorem -> axiom 改写现在使用 `macro_rules (kind := Lean.Parser.Command.declaration)`
- `temporary_axiom` 保留显式 attribute syntax，并通过 `public meta initialize` 注册
- `temporary_axiom` 的 extension 与查询也走 `public meta initialize` / `meta def`

直接结果：

- 下游模块 import 后能立即识别 `@[temporary_axiom]`
- 真实失败点会落在“未批准”或 “hash 不匹配”，而不是 `Unknown attribute`

### 生成器与 registry 输出

- 生成的 Lean shard / aggregate 文件统一使用 `module` 头
- 共享声明统一输出为 `public import` / `public def`
- `approve` / `audit` 使用的临时 probe 文件会生成合法模块名
- `audit-temporary-axioms` 使用的临时审计文件也带 `module` 头

### CLI 与数据模型

- 保留一个 Python 管理入口，不再依赖 shell 包装脚本
- 审核元数据收敛为 `status` 与 `commit`
- `commit` 默认覆盖，显式 `--append` 才追加
- `status` 与 `commit` 变更不会写入 `history/`
- `history/` 只在 `statement_hash` 变化时新增记录

## 对下游项目最直接的影响

- 如果你缓存了旧的 generated Lean 文件，更新后要重新运行 `generate`
- 如果你以前引用过已删除的 shell 脚本，CI 必须改
- 如果你以前假设 `TemporaryAxiomTool/TemporaryAxiom.lean` 同时承载 runtime extension，现在应按新文件结构同步
- 如果你以前假设 `ApprovedStatementRegistry.lean` 内还包含 hash 实现，现在应把 `Hash.lean` 一并同步

## 本仓库已完成的回归验证

- `lake build TemporaryAxiomTool`
- `python3 -m py_compile scripts/manage_approved_statement_registry.py scripts/registry_tool/*.py`
- `python3 scripts/manage_approved_statement_registry.py audit`
- `python3 scripts/manage_approved_statement_registry.py audit-temporary-axioms --module TemporaryAxiomTool`
- 一个临时 smoke module：
  - 已确认下游模块中 `temporary_axiom` 不再报 `Unknown attribute`
  - 当前空数据库下会按预期报“声明不在 approved statement registry”
