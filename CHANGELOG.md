# Changelog

当前仓库从 `0.1.0` 开始进行显式版本管理；更早的演进历史以 Git 提交记录为准。

## [0.1.0] - 2026-04-01

首个显式版本化发布。

### Added

- 新增 `VERSION`
- 新增 `CHANGELOG.md`
- 新增 [TemporaryAxiomTool/ApprovedStatementRegistry/Hash.lean](./TemporaryAxiomTool/ApprovedStatementRegistry/Hash.lean)
- 新增 [TemporaryAxiomTool/TemporaryAxiom/Runtime.lean](./TemporaryAxiomTool/TemporaryAxiom/Runtime.lean)
- 新增 [docs/downstream_upgrade_note.md](./docs/downstream_upgrade_note.md)

### Changed

- `temporary_axiom` 的 theorem -> axiom 改写改为 `macro_rules (kind := Lean.Parser.Command.declaration)`
- `temporary_axiom` 保留显式 attribute syntax，并通过 `public meta initialize` 注册
- `temporary_axiom` 的 extension 与查询拆到独立 runtime 模块
- statement hash 计算拆到独立 `Hash.lean`
- 生成的 Lean shard / aggregate / probe / 临时审计文件统一使用 `module` 头
- CLI 继续保持单一 Python 入口，不再依赖 shell 包装脚本
- 审核元数据收敛为 `status` 与 `commit`
- 文档分工重新收束为 README / 技术规格 / 数据库格式 / 升级说明 / CHANGELOG 五层

### Fixed

- 修复下游模块 import 后 `temporary_axiom` 可能报 `Unknown attribute` 的问题
- 修复 tool 在 Lean module system 下的 phase / init 导出链路
- 修复 CI 仍引用已删除 shell 审计脚本时的升级路径
- 修复 `approve --module` 单值参数被按字符去重的问题

### Breaking

- 当前版本要求 downstream 已迁移到 Lean module system
- 旧式非 `module` 宿主项目不在支持范围内
- `approve`、`audit`、`audit-temporary-axioms` 生成的临时 Lean 文件本身带 `module` 头，因此只能 import module-system 宿主模块
- 下游若从旧式显式 namespace 文件迁移到 `module` 文件，需要重新核对 `decl_name`、`--module` 参数和 registry 旧条目
- 已删除 `./scripts/run_approved_statement_registry_audit.sh`，统一改用 `python3 scripts/manage_approved_statement_registry.py ...`
