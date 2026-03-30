# TemporaryAxiomTool

`TemporaryAxiomTool` 是一个可迁移的 Lean 工具包，用于并行形式化中的“定理临时公理化”工作流。

它提供三部分能力：

- Lean 侧 `@[temporary_axiom]` 宏与即时校验
- 外部已批准陈述注册库与历史/归档机制
- 审计、清理与 CI/CD 接入脚本

这个分支已经剔除了测试项目本身，只保留工具骨架、空注册库模板、脚本和文档，适合作为其他形式化项目的部署源。

## 仓库结构

- [TemporaryAxiomTool.lean](TemporaryAxiomTool.lean)
- [TemporaryAxiomTool/TemporaryAxiom.lean](TemporaryAxiomTool/TemporaryAxiom.lean)
- [TemporaryAxiomTool/ApprovedStatementRegistry.lean](TemporaryAxiomTool/ApprovedStatementRegistry.lean)
- [TemporaryAxiomTool/ApprovedStatementRegistry/Types.lean](TemporaryAxiomTool/ApprovedStatementRegistry/Types.lean)
- [TemporaryAxiomTool/ApprovedStatementRegistry/Generated.lean](TemporaryAxiomTool/ApprovedStatementRegistry/Generated.lean)
- [TemporaryAxiomTool/ApprovedStatementRegistry/Shards/](TemporaryAxiomTool/ApprovedStatementRegistry/Shards/)
- [approved_statement_registry_db/](approved_statement_registry_db/)
- [scripts/manage_approved_statement_registry.py](scripts/manage_approved_statement_registry.py)
- [scripts/run_approved_statement_registry_audit.sh](scripts/run_approved_statement_registry_audit.sh)
- [scripts/run_temporary_axiom_audit.sh](scripts/run_temporary_axiom_audit.sh)
- [scripts/cleanup_temporary_axiom_scaffolding.py](scripts/cleanup_temporary_axiom_scaffolding.py)
- [templates/TemporaryAxiomAudit.lean](templates/TemporaryAxiomAudit.lean)
- [docs/temporary_axiom.md](docs/temporary_axiom.md)

## 快速部署

推荐把以下路径复制或同步到宿主 Lean 项目根目录：

- `TemporaryAxiomTool.lean`
- `TemporaryAxiomTool/`
- `approved_statement_registry_db/`
- `scripts/manage_approved_statement_registry.py`
- `scripts/run_approved_statement_registry_audit.sh`
- `scripts/run_temporary_axiom_audit.sh`
- `scripts/cleanup_temporary_axiom_scaffolding.py`
- `templates/TemporaryAxiomAudit.lean`
- `docs/temporary_axiom.md`

然后在宿主项目的 `lakefile.toml` 中加入一个额外的 `lean_lib`：

```toml
[[lean_lib]]
name = "TemporaryAxiomTool"
```

如果宿主项目原本没有 `approved_statement_registry_db/`，直接保留这里的空目录模板即可。

## 宿主项目接入

在需要跳过证明的 Lean 文件中：

```lean
import TemporaryAxiomTool.TemporaryAxiom
```

把需要跳过的 theorem 写成：

```lean
@[temporary_axiom]
theorem YourProject.someTheorem (h : P ∧ Q) : Q ∧ P := by
  sorry
```

但只有在该 theorem 的陈述已经被批准写入注册库后，这个标签才会通过校验。

## 审计文件

从模板复制一个宿主项目自己的审计文件，例如：

```bash
cp templates/TemporaryAxiomAudit.lean TemporaryAxiomAudit.lean
```

然后在这个文件里额外 import 你的项目根模块或相关 section 模块。最小形式类似：

```lean
import YourProject
import TemporaryAxiomTool.TemporaryAxiom

#assert_no_temporary_axioms
```

对应 shell 审计脚本：

```bash
./scripts/run_temporary_axiom_audit.sh TemporaryAxiomAudit.lean
```

如果不传参数，脚本会默认寻找根目录下的 `TemporaryAxiomAudit.lean`。

## 注册库管理

统一入口：

- [scripts/manage_approved_statement_registry.py](scripts/manage_approved_statement_registry.py)

最常用命令：

```bash
python3 scripts/manage_approved_statement_registry.py approve \
  --module YourProject.Section2 \
  --chapter 3 \
  --section 2 \
  --decl YourProject.someTheorem
```

```bash
python3 scripts/manage_approved_statement_registry.py audit
```

```bash
python3 scripts/manage_approved_statement_registry.py history --include-archive
```

```bash
python3 scripts/manage_approved_statement_registry.py rollback --event-id <EVENT_ID>
```

## 文档

- 工具工作流说明：[docs/temporary_axiom.md](docs/temporary_axiom.md)
- 注册库数据库格式：[approved_statement_registry_db/README.md](approved_statement_registry_db/README.md)

## 本仓库的定位

这个仓库现在是“工具源仓库”，不是业务形式化工程本体。

因此：

- 默认注册库是空的
- `Generated.lean` 是空聚合文件
- `Shards/`、`current/`、`history/`、`archive/` 只保留模板目录
- 宿主项目的 theorem、section 与最终主定理不再包含在这里
