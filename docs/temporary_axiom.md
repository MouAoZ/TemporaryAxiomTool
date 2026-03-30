# TemporaryAxiomTool 工作流说明

## 目标

`TemporaryAxiomTool` 用于并行形式化中的“定理陈述先批准，证明后补齐”工作流。

核心原则：

1. 先把可能被下游依赖的 theorem statement 形式化并批准入库
2. 只有确实需要并行跳过时，才为该 theorem 添加 `@[temporary_axiom]`
3. `@[temporary_axiom]` 只允许用于已批准陈述

因此，这个工具解决的是“证明暂时缺失，但陈述已经可信”的场景，而不是让任意 theorem 都能被静默跳过。

## 总体架构

工具由三层组成：

1. Lean 侧临时公理工具：
   [TemporaryAxiomTool/TemporaryAxiom.lean](../TemporaryAxiomTool/TemporaryAxiom.lean)
2. Lean 侧批准注册表：
   [TemporaryAxiomTool/ApprovedStatementRegistry.lean](../TemporaryAxiomTool/ApprovedStatementRegistry.lean)
3. 外部注册库数据库：
   [approved_statement_registry_db/README.md](../approved_statement_registry_db/README.md)

职责划分：

- `TemporaryAxiom` 负责 theorem -> axiom 改写与即时合法性检查
- `ApprovedStatementRegistry` 负责 statement hash、probe 命令和查找表装配
- 外部数据库负责批准记录、历史事件、review note 与归档

## Lean 侧工作流

当 Lean 读到：

```lean
@[temporary_axiom]
theorem YourProject.someTheorem (h : P ∧ Q) : Q ∧ P := by
  sorry
```

处理顺序如下：

1. parser 读入完整 `declaration`
2. command macro 发现该 theorem 带有 `@[temporary_axiom]`
3. macro 丢弃证明体，仅保留声明头，并将 `theorem` 改写为 `axiom`
4. Lean 对改写后的声明正常 elaboration
5. attribute 在 `afterTypeChecking` 阶段运行
6. 工具读取环境中已生成的批准注册表，检查：
   - 声明名是否已批准
   - 当前 elaborated statement hash 是否与批准记录一致

若不满足条件，报错会在该声明处立刻跳出。

## import 规则

宿主项目普通文件不需要额外 import。

只有需要使用 `@[temporary_axiom]` 的文件需要：

```lean
import TemporaryAxiomTool.TemporaryAxiom
```

这样可以把工具依赖限制在真正需要跳过证明的模块里。

## 已批准陈述注册库

外部数据库位于：

- `approved_statement_registry_db/current/`
- `approved_statement_registry_db/history/`
- `approved_statement_registry_db/archive/`

Lean 侧自动生成文件位于：

- `TemporaryAxiomTool/ApprovedStatementRegistry/Generated.lean`
- `TemporaryAxiomTool/ApprovedStatementRegistry/Shards/`

数据库格式详见：

- [approved_statement_registry_db/README.md](../approved_statement_registry_db/README.md)

## 管理脚本

统一入口：

- [scripts/manage_approved_statement_registry.py](../scripts/manage_approved_statement_registry.py)

核心命令：

- `approve`: 冻结一个或多个 theorem 的当前陈述
- `commit`: 给已批准定理追加 review note
- `prune`: 从注册库移除定理
- `rollback`: 回滚某个历史事件
- `history`: 查看或归档历史
- `audit`: 对照当前 Lean 环境做 hash 审计
- `generate`: 仅根据 current 快照重建 Lean 侧文件

查看完整参数：

```bash
python3 scripts/manage_approved_statement_registry.py --help
python3 scripts/manage_approved_statement_registry.py approve --help
```

### 典型 `approve`

```bash
python3 scripts/manage_approved_statement_registry.py approve \
  --module YourProject.Section2 \
  --chapter 3 \
  --section 2 \
  --decl YourProject.someTheorem
```

### 典型 `commit`

```bash
python3 scripts/manage_approved_statement_registry.py commit \
  --decl YourProject.someTheorem \
  --severity warning \
  --message "binder names changed recently; manual review recommended"
```

### 典型 `audit`

```bash
python3 scripts/manage_approved_statement_registry.py audit
```

### 典型 `history`

```bash
python3 scripts/manage_approved_statement_registry.py history --include-archive
```

### 典型 `rollback`

```bash
python3 scripts/manage_approved_statement_registry.py rollback \
  --event-id 20260331T010203Z_approve_ab12cd34
```

## 审计与 CI/CD

注册库 hash 审计：

```bash
./scripts/run_approved_statement_registry_audit.sh
```

临时公理清理审计：

```bash
./scripts/run_temporary_axiom_audit.sh TemporaryAxiomAudit.lean
```

建议在宿主项目 CI 中加入两个检查：

1. registry 审计，确保已批准陈述未漂移
2. 最终清理阶段的 `#assert_no_temporary_axioms`

## 清理工具

当宿主项目准备移除全部脚手架时，可使用：

- [scripts/cleanup_temporary_axiom_scaffolding.py](../scripts/cleanup_temporary_axiom_scaffolding.py)

它会：

- 扫描残留的 `@[temporary_axiom]`
- 在确认已经清空后，删除工具 import、注册表文件、数据库、审计脚本与文档块
- 可选执行清理后的 `lake build`

常见用法：

```bash
python3 scripts/cleanup_temporary_axiom_scaffolding.py --execute
```

## 部署建议

建议把本仓库当作“工具源”来同步到宿主项目，而不是要求用户手工重命名模块前缀。

宿主项目只需：

1. 复制 `TemporaryAxiomTool/`、`approved_statement_registry_db/`、`scripts/`、`templates/`
2. 在宿主 `lakefile.toml` 里新增 `[[lean_lib]] name = "TemporaryAxiomTool"`
3. 在业务证明文件里 `import TemporaryAxiomTool.TemporaryAxiom`
4. 创建自己的 `TemporaryAxiomAudit.lean`

这样可以最大程度降低接入成本与后续维护成本。
