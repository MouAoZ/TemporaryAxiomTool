# TemporaryAxiomTool

`TemporaryAxiomTool` 是一个面向 Lean 4 并行形式化的工具库，用来管理“先冻结 theorem statement，后补证明”的工作流。

它的核心约束是：

- 只有已经进入批准注册库的陈述，才允许被标记为 `@[temporary_axiom]`
- Lean 会在声明通过 elaboration 后立即校验 statement hash，防止陈述漂移
- 人工审核元数据与 statement 版本历史分离管理
- 项目收尾时必须通过审计确认临时公理全部移除

这个仓库提供的是可复用工具本体，而不是某个具体形式化项目的示例工程。

## 文档导航

- [docs/temporary_axiom.md](docs/temporary_axiom.md): 完整技术规格、工具行为说明、命令参数说明与示例
- [approved_statement_registry_db/README.md](approved_statement_registry_db/README.md): 注册库数据库格式、字段语义与 history 规则
- [docs/downstream_upgrade_note.md](docs/downstream_upgrade_note.md): 下游项目从旧版本升级时的迁移步骤与测试清单
- [CHANGELOG.md](CHANGELOG.md): 版本变化、breaking changes 与外部引用变化

README 只保留快速接入、最小工作流和仓库结构；命令细节、数据库 schema 与升级回归要求分别放在上面的专门文档里。

## 环境要求

- 当前版本: `0.1.0`
- Lean `v4.29.0-rc8`
- 宿主项目已采用 Lean module system
  - 业务 Lean 文件应使用 `module` 头
  - 当前版本不支持旧式非 `module` downstream

工具本体当前不依赖额外的 Lake 包；依赖声明见 [lakefile.toml](lakefile.toml) 与 [lean-toolchain](lean-toolchain)。

如果宿主项目本身依赖 `mathlib4` 或其他包，应继续由宿主项目自行声明和管理。

## 接入宿主项目

当前仓库的设计偏向将工具文件直接同步到宿主 Lean 项目根目录。最小接入集合通常包括：

- `TemporaryAxiomTool.lean`
- `TemporaryAxiomTool/`
- `approved_statement_registry_db/`
- `scripts/manage_approved_statement_registry.py`
- `scripts/registry_tool/`

然后在宿主项目的 `lakefile.toml` 中加入：

```toml
[[lean_lib]]
name = "TemporaryAxiomTool"
```

首次接入后建议执行：

```bash
lake build TemporaryAxiomTool
```

如果宿主项目仍是旧式非 `module` 文件布局，先不要直接接入当前版本。
当前脚本生成的 probe / 临时审计文件本身就是 `module` 文件，因此它们只能 import 同样采用 module system 的宿主模块。

如果你是在已有宿主项目里同步更新本工具，而不是初次接入，建议额外执行：

```bash
python3 scripts/manage_approved_statement_registry.py generate
lake build TemporaryAxiomTool
```

完整升级步骤见 [docs/downstream_upgrade_note.md](docs/downstream_upgrade_note.md)。

## 最小使用方式

只有需要跳过证明的 Lean 文件才需要导入：

```lean
import TemporaryAxiomTool.TemporaryAxiom
```

将 theorem 写成：

```lean
@[temporary_axiom]
theorem YourProject.someTheorem (h : P ∧ Q) : Q ∧ P := by
  -- 证明体只要求能通过语法解析
  ...
```

该声明会在语法层被改写为 `axiom`，但只有当下面两项都成立时才会通过校验：

- `YourProject.someTheorem` 已经存在于已批准陈述注册库中
- 当前 elaborated statement hash 与批准记录一致

## 常用工作流

1. 批准陈述：

```bash
python3 scripts/manage_approved_statement_registry.py approve \
  --module YourProject.Section2 \
  --chapter 3 \
  --section 2 \
  --decl YourProject.someTheorem
```

2. 在需要时写入审核元数据：

```bash
python3 scripts/manage_approved_statement_registry.py commit \
  --decl YourProject.someTheorem \
  --status needs_attention \
  --message "binder order changed; manual review suggested"
```

3. 为审核者打印当前条目：

```bash
python3 scripts/manage_approved_statement_registry.py report
```

4. 审计 current 快照与 Lean 环境：

```bash
python3 scripts/manage_approved_statement_registry.py audit
```

5. 发布前审计是否还残留 `temporary_axiom`：

```bash
python3 scripts/manage_approved_statement_registry.py audit-temporary-axioms \
  --module YourProject
```

所有子命令的完整参数说明与更多示例见 [docs/temporary_axiom.md](docs/temporary_axiom.md)。

## 仓库结构

- [TemporaryAxiomTool.lean](TemporaryAxiomTool.lean): 库根模块
- [TemporaryAxiomTool/TemporaryAxiom.lean](TemporaryAxiomTool/TemporaryAxiom.lean): `@[temporary_axiom]` 宏、属性与审计命令
- [TemporaryAxiomTool/TemporaryAxiom/Runtime.lean](TemporaryAxiomTool/TemporaryAxiom/Runtime.lean): `temporary_axiom` 运行时 extension 与环境查询
- [TemporaryAxiomTool/ApprovedStatementRegistry.lean](TemporaryAxiomTool/ApprovedStatementRegistry.lean): 注册表入口
- [TemporaryAxiomTool/ApprovedStatementRegistry/Types.lean](TemporaryAxiomTool/ApprovedStatementRegistry/Types.lean): 运行时批准条目类型
- [TemporaryAxiomTool/ApprovedStatementRegistry/Hash.lean](TemporaryAxiomTool/ApprovedStatementRegistry/Hash.lean): statement hash 计算逻辑
- [TemporaryAxiomTool/ApprovedStatementRegistry/Generated.lean](TemporaryAxiomTool/ApprovedStatementRegistry/Generated.lean): 自动生成的注册表聚合模块
- [approved_statement_registry_db/](approved_statement_registry_db/): 外部注册库数据库
- [scripts/manage_approved_statement_registry.py](scripts/manage_approved_statement_registry.py): 唯一 CLI 入口

当前 Lean 侧实现已经整理为兼容 Lean 4 module system 的结构；变更摘要见 [CHANGELOG.md](CHANGELOG.md)。

## 当前仓库状态

这是一个工具源仓库，因此默认只保留可复用骨架：

- `approved_statement_registry_db/current/` 与 `history/` 为空模板目录
- [TemporaryAxiomTool/ApprovedStatementRegistry/Generated.lean](TemporaryAxiomTool/ApprovedStatementRegistry/Generated.lean) 是空聚合入口
- 仓库中不包含任何宿主项目的 theorem 数据或业务形式化内容
