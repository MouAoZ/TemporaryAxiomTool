# TemporaryAxiomTool

`TemporaryAxiomTool` 是一个面向 Lean 4 并行形式化的工具库，用来管理“先冻结 theorem statement，后补证明”的工作流。

它的核心约束是：

- 只有已经进入批准注册库的陈述，才允许被标记为 `@[temporary_axiom]`
- Lean 会在声明通过 elaboration 后立即校验 statement hash，防止陈述漂移
- 人工审核元数据与 statement 版本历史分离管理
- 项目收尾时必须通过审计确认临时公理全部移除

这个仓库提供的是可复用工具本体，而不是某个具体形式化项目的示例工程。

完整技术规格见 [docs/temporary_axiom.md](docs/temporary_axiom.md)，
数据库格式说明见 [approved_statement_registry_db/README.md](approved_statement_registry_db/README.md)。

## 核心能力

- Lean 侧 `@[temporary_axiom]` 宏、属性校验与最终审计命令
- 外部已批准陈述注册库，以及对应的 Lean 侧自动生成 registry 模块
- 单入口管理脚本，支持 `approve`、`commit`、`report`、`audit`、`history`
- statement hash 变化历史记录，与人工审核元数据分离

## 环境要求

- Lean `v4.29.0-rc8`

工具本体当前不依赖额外的 Lake 包；依赖声明见 [lakefile.toml](lakefile.toml) 与 [lean-toolchain](lean-toolchain)。

如果宿主项目本身依赖 `mathlib4` 或其他包，应继续由宿主项目自行声明和管理。

## 仓库结构

- [TemporaryAxiomTool.lean](TemporaryAxiomTool.lean): 库根模块
- [TemporaryAxiomTool/TemporaryAxiom.lean](TemporaryAxiomTool/TemporaryAxiom.lean): `@[temporary_axiom]` 宏、属性与审计命令
- [TemporaryAxiomTool/ApprovedStatementRegistry.lean](TemporaryAxiomTool/ApprovedStatementRegistry.lean): 注册表入口
- [TemporaryAxiomTool/ApprovedStatementRegistry/Types.lean](TemporaryAxiomTool/ApprovedStatementRegistry/Types.lean): 注册表数据类型
- [TemporaryAxiomTool/ApprovedStatementRegistry/Generated.lean](TemporaryAxiomTool/ApprovedStatementRegistry/Generated.lean): 自动生成的注册表聚合模块
- [approved_statement_registry_db/](approved_statement_registry_db/): 外部注册库数据库
- [scripts/manage_approved_statement_registry.py](scripts/manage_approved_statement_registry.py): 唯一 CLI 入口
- [scripts/registry_tool/](scripts/registry_tool/): CLI 的内部实现模块
- [docs/temporary_axiom.md](docs/temporary_axiom.md): 完整技术规格与工作流说明

## 接入宿主项目

当前仓库的设计偏向将工具文件直接同步到宿主 Lean 项目根目录。最小接入集合通常包括：

- `TemporaryAxiomTool.lean`
- `TemporaryAxiomTool/`
- `approved_statement_registry_db/`
- `scripts/manage_approved_statement_registry.py`
- `scripts/registry_tool/`
- `docs/temporary_axiom.md`

然后在宿主项目的 `lakefile.toml` 中加入：

```toml
[[lean_lib]]
name = "TemporaryAxiomTool"
```

接入后建议先构建一次：

```bash
lake build TemporaryAxiomTool
```

管理脚本会根据它自身所在路径自动定位项目根目录，因此正常使用时不需要再额外指定项目根路径参数。

## 最小使用方式

只有需要跳过证明的 Lean 文件才需要导入：

```lean
import TemporaryAxiomTool.TemporaryAxiom
```

将 theorem 写成：

```lean
@[temporary_axiom]
theorem YourProject.someTheorem (h : P ∧ Q) : Q ∧ P := by
  sorry
```

该声明会在语法层被改写为 `axiom`，但只有当下面两项都成立时才会通过校验：

- `YourProject.someTheorem` 已经存在于已批准陈述注册库中
- 当前 elaborated statement hash 与批准记录一致

## 典型工作流

### 1. 批准陈述

```bash
python3 scripts/manage_approved_statement_registry.py approve \
  --module YourProject.Section2 \
  --chapter 3 \
  --section 2 \
  --decl YourProject.someTheorem
```

同一条命令可以重复传入多个不同的 `--decl`，但不允许把同一个声明名重复传入两次。

### 2. 在需要时写入审核元数据

默认覆盖该条目的 `commit` 列表，并可同步设置 `status`：

```bash
python3 scripts/manage_approved_statement_registry.py commit \
  --decl YourProject.someTheorem \
  --status needs_attention \
  --message "binder order changed; manual review suggested"
```

需要增量追加时显式使用 `--append`：

```bash
python3 scripts/manage_approved_statement_registry.py commit \
  --decl YourProject.someTheorem \
  --append \
  --message "secondary reviewer confirmed issue scope"
```

清空全部评论：

```bash
python3 scripts/manage_approved_statement_registry.py commit \
  --decl YourProject.someTheorem \
  --clear
```

### 3. 为人工审核者生成报告

默认在不额外传入 `--all`、`--decl`、`--status` 时，只打印 `commit` 非空的条目：

```bash
python3 scripts/manage_approved_statement_registry.py report
```

如需完整查看，可使用：

```bash
python3 scripts/manage_approved_statement_registry.py report --all --verbose --lifecycle
```

### 4. 审计注册库

```bash
python3 scripts/manage_approved_statement_registry.py audit
```

如果想把非安全状态直接视为失败，可使用：

```bash
python3 scripts/manage_approved_statement_registry.py audit --fail-on-status needs_attention
```

### 5. 查看 statement 变化历史

```bash
python3 scripts/manage_approved_statement_registry.py history
```

只看某个声明：

```bash
python3 scripts/manage_approved_statement_registry.py history \
  --decl YourProject.someTheorem \
  --verbose
```

### 6. 审计剩余临时公理

```bash
python3 scripts/manage_approved_statement_registry.py audit-temporary-axioms \
  --module YourProject
```

若项目没有单一根模块，可重复传入多个不同的 `--module`。

### 7. 修复生成文件

正常工作流里，`approve` 和 `prune` 会自动重建 Lean 侧 registry 文件。

如果你只是想根据当前 `approved_statement_registry_db/current/` 重新生成 Lean 侧文件，可使用：

```bash
python3 scripts/manage_approved_statement_registry.py generate
```

### 8. 最终手动清理脚手架

当前版本不再提供自动 cleanup 脚本。推荐在最终收尾阶段完成下面几步：

1. 确认 `audit-temporary-axioms` 已通过
2. 删除业务文件中不再需要的 `import TemporaryAxiomTool...`
3. 从 `lakefile.toml` 中移除 `TemporaryAxiomTool` 对应的 `lean_lib`
4. 删除工具目录、注册库目录和本仓库同步进去的脚本
5. 重新 `lake build` 验证宿主项目已摆脱工具依赖

## 命令概览

[scripts/manage_approved_statement_registry.py](scripts/manage_approved_statement_registry.py) 提供统一入口，常用子命令如下：

- `approve`: 将当前 theorem statement 批准写入指定 chapter/section 分片
- `commit`: 更新人工审核 `commit` 与 `status`
- `report`: 为人工审核者打印当前条目
- `audit`: 对照当前 Lean 环境核对 statement hash
- `audit-temporary-axioms`: 临时生成审计入口并运行 `#assert_no_temporary_axioms`
- `prune`: 从注册库中移除不再可信的陈述
- `history`: 查看 statement hash 变化历史
- `generate`: 仅根据 `approved_statement_registry_db/current/` 重建 Lean 侧生成文件

查看完整参数：

```bash
python3 scripts/manage_approved_statement_registry.py --help
python3 scripts/manage_approved_statement_registry.py approve --help
python3 scripts/manage_approved_statement_registry.py commit --help
```

## 当前仓库状态

这是一个工具源仓库，因此默认只保留可复用骨架：

- `approved_statement_registry_db/current/` 与 `history/` 为空模板目录
- [TemporaryAxiomTool/ApprovedStatementRegistry/Generated.lean](TemporaryAxiomTool/ApprovedStatementRegistry/Generated.lean) 是空聚合入口
- 仓库中不包含任何宿主项目的 theorem 数据或业务形式化内容
