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
- `TemporaryAxiomTool/ApprovedStatementRegistry/Shards/` 下的内部 shard 模块

其中：

- `Generated.lean` 是聚合入口，负责 import 各个 chapter/section 分片并导出总表
- `Shards/` 是脚本自动生成的内部拆分目录；当 current 快照为空时，这个目录可能暂时不存在

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

通用参数：

- `--project-root` : Lean 项目根目录路径；接受文件系统路径字符串，默认当前目录 `.`；所有子命令都支持，仅在工具目录不位于当前工作目录时需要显式传入。

### 典型 `approve`

- `--module` : 用于探测声明的 Lean 模块名；接受 Lean import 路径字符串，例如 `YourProject.Section2`，而不是文件路径；单次命令只接受一个模块，该模块需要能导入本次所有 `--decl`。
- `--chapter` : 注册库分片所属 chapter 编号；接受十进制整数；单值输入；决定 JSON shard 与生成条目的章节索引。
- `--section` : 注册库分片所属 section 编号；接受十进制整数；单值输入；与 `--chapter` 一起确定目标 shard。
- `--decl` : 要批准的定理名；接受 Lean 全限定声明名字符串，例如 `YourProject.someTheorem`, 不推荐短名；支持重复传入；同一命令可一次批准同模块、同 chapter/section 下的多个定理。
- `--reason` : 审批原因；接受任意简短字符串；单值输入，可省略；省略时默认写入 `approved statement freeze`。
- `--author` : 历史记录中的操作者；接受字符串；单值输入，可省略；省略时默认写入 `ai-agent`。
- `--skip-build` : 布尔开关；不接受额外参数；传入后只更新 JSON 与生成文件，不执行 `lake build TemporaryAxiomTool.ApprovedStatementRegistry`。

```bash
python3 scripts/manage_approved_statement_registry.py approve \
  --module YourProject.Section2 \
  --chapter 3 \
  --section 2 \
  --decl YourProject.someTheorem
```

### 典型 `commit`

- `--decl` : 已批准定理名；接受 Lean 全限定声明名字符串；支持重复传入；可一次给多个已批准定理追加同一条 review note。
- `--message` : review note 内容；接受字符串；单值输入，必填；用于记录人工关注点、告警或审查意见。
- `--severity` : review note 严重级别；接受 `comment`、`warning`、`alert` 三选一；单值输入，可省略；默认值为 `warning`。
- `--reason` : history 中记录的事件原因；接受字符串；单值输入，可省略；省略时会退回使用 `--message` 内容作为事件原因。
- `--author` : 历史记录中的操作者；接受字符串；单值输入，可省略；省略时默认写入 `ai-agent`。

```bash
python3 scripts/manage_approved_statement_registry.py commit \
  --decl YourProject.someTheorem \
  --severity warning \
  --message "binder names changed recently; manual review recommended"
```

### 典型 `prune`

- `--decl` : 要从已批准陈述注册库中移除的定理名；接受 Lean 全限定声明名字符串；支持重复传入；适合批量剔除同一轮不再可信的陈述。
- `--reason` : 移除原因；接受字符串；单值输入，可省略；省略时默认写入 `removed from approved statement registry`。
- `--author` : 历史记录中的操作者；接受字符串；单值输入，可省略；省略时默认写入 `ai-agent`。
- `--skip-build` : 布尔开关；不接受额外参数；传入后跳过 registry Lean 侧重建与构建。

```bash
python3 scripts/manage_approved_statement_registry.py prune \
  --decl YourProject.someTheorem
```

### 典型 `audit`

- `--decl` : 审计范围过滤；接受 Lean 全限定声明名字符串；支持重复传入；省略时审计当前注册库中的全部定理。
- `--fail-on-review-status` : review 状态阈值；接受 `comment`、`warning`、`alert` 三选一；单值输入，可省略；传入后，如果任一被选中定理的 `review_status` 不低于该阈值，则命令以失败退出，适合接入 CI。

```bash
python3 scripts/manage_approved_statement_registry.py audit
```

### 典型 `history`

- `--decl` : history 过滤条件；接受 Lean 全限定声明名字符串；支持重复传入；在浏览模式下用于缩小输出范围，在归档模式下用于选择待归档事件。
- `--limit` : 输出条数上限；接受十进制整数；单值输入，可省略；仅用于普通 history 浏览，不可与 `--archive` 混用。
- `--include-archive` : 布尔开关；不接受额外参数；浏览 history 时同时读取 `archive/` 中的归档包。
- `--archive-only` : 布尔开关；不接受额外参数；浏览时只显示归档包中的事件。
- `--archive` : 布尔开关；不接受额外参数；把匹配到的 live history 事件打包归档到 `approved_statement_registry_db/archive/`。
- `--archive-all` : 布尔开关；不接受额外参数；仅在 `--archive` 模式下使用；表示归档全部 live history，而不是用 `--decl` 选择子集。
- `--reason` : 归档原因；接受字符串；单值输入，可省略；仅在 `--archive` 模式下写入 archive bundle 元数据；省略时脚本会自动生成默认说明。
- `--author` : 归档操作者；接受字符串；单值输入，可省略；默认 `ai-agent`；仅在 `--archive` 模式下写入 archive bundle 元数据。
- `--execute` : 布尔开关；不接受额外参数；仅在 `--archive` 模式下有意义；不传时 `--archive` 只做 dry run。

```bash
python3 scripts/manage_approved_statement_registry.py history --include-archive
```

典型归档：

```bash
python3 scripts/manage_approved_statement_registry.py history \
  --archive \
  --decl YourProject.someTheorem \
  --execute
```

### 典型 `rollback`

- `--event-id` : 要回滚的历史事件编号；接受完整 event id 字符串，例如 `20260331T010203Z_approve_ab12cd34`；单值输入；会同时在 live history 与 archive 中查找。
- `--reason` : 回滚原因；接受字符串；单值输入，可省略；省略时默认写入 `rollback of <EVENT_ID>`。
- `--author` : 历史记录中的操作者；接受字符串；单值输入，可省略；默认 `ai-agent`。
- `--skip-build` : 布尔开关；不接受额外参数；传入后跳过回滚后的 Lean registry 构建。

```bash
python3 scripts/manage_approved_statement_registry.py rollback \
  --event-id 20260331T010203Z_approve_ab12cd34
```

### 典型 `generate`

- `--skip-build` : 布尔开关；不接受额外参数；传入后只根据 `approved_statement_registry_db/current/` 重写生成文件，不执行后续 `lake build`。

```bash
python3 scripts/manage_approved_statement_registry.py generate
```

## 审计与 CI/CD

注册库 hash 审计：

```bash
./scripts/run_approved_statement_registry_audit.sh
```

临时公理清理审计：

```bash
./scripts/run_temporary_axiom_audit.sh --module YourProject
```

这个脚本会临时生成一个 Lean 审计入口，并导入你通过 `--module` 传入的宿主模块。
如果项目没有单一根模块，可以重复传入多个 `--module`。

参数说明：

- `--module` : 要导入到临时审计入口中的 Lean 模块；接受 Lean import 路径字符串；支持重复传入；至少需要提供一个，推荐传项目根模块或所有可能引入 `temporary_axiom` 的 section 根模块。
- `TEMPORARY_AXIOM_KEEP_GENERATED_AUDIT=1` : 环境变量开关；不属于命令行参数；设置后保留脚本生成的临时审计 `.lean` 文件，便于排查 import 或环境问题。

建议在宿主项目 CI 中加入两个检查：

1. registry 审计，确保已批准陈述未漂移
2. 最终清理阶段的 `#assert_no_temporary_axioms`

例如：

```yaml
- name: Temporary axiom audit
  run: ./scripts/run_temporary_axiom_audit.sh --module YourProject
```

如果需要覆盖多个 section 根模块：

```yaml
- name: Temporary axiom audit
  run: |
    ./scripts/run_temporary_axiom_audit.sh \
      --module YourProject.Section2 \
      --module YourProject.Section3
```

如果你希望 cleanup 脚本后续自动移除这些审计步骤，请把相关 workflow block
包在下面的 marker 中：

```yaml
# approved-statement-registry-audit:start
- name: Approved statement registry audit
  run: ./scripts/run_approved_statement_registry_audit.sh
# approved-statement-registry-audit:end

# temporary-axiom-audit:start
- name: Temporary axiom audit
  run: ./scripts/run_temporary_axiom_audit.sh --module YourProject
# temporary-axiom-audit:end
```

如果 workflow 里仍然存在未加 marker 的审计脚本调用，cleanup 会直接报错并拒绝继续，
避免在删除脚手架后留下失效的 CI 引用。

## 清理工具

当宿主项目准备移除全部脚手架时，可使用：

- [scripts/cleanup_temporary_axiom_scaffolding.py](../scripts/cleanup_temporary_axiom_scaffolding.py)

它会：

- 扫描残留的 `@[temporary_axiom]`
- 在确认已经清空后，删除工具 import、注册表文件、数据库、审计脚本与文档块
- 同步清理 `lakefile.toml` 中 `TemporaryAxiomTool` 对应的 `lean_lib` 与 `defaultTargets` 引用
- 在执行真实删除前先运行一次模块级审计
- 修改失败时自动回滚文件与目录改动
- 可选执行清理后的 `lake build`

常见用法：

```bash
python3 scripts/cleanup_temporary_axiom_scaffolding.py \
  --audit-module YourProject \
  --execute
```

如果项目没有单一根模块，可以重复传入多个 `--audit-module`。
只有显式加上 `--skip-audit` 时，脚本才会跳过这一步预检查。

关键参数：

- `--project-root` : 要清理的 Lean 项目根目录；接受文件系统路径字符串；单值输入；默认当前目录。
- `--audit-module` : 清理前预审计要导入的 Lean 模块；接受 Lean import 路径字符串；支持重复传入；除非使用 `--skip-audit`，否则至少应提供一个。
- `--workflow-file` : 要扫描与清理的 workflow 文件；接受相对项目根目录的路径字符串；支持重复传入；省略时默认扫描 `.github/workflows/` 下全部 `.yml/.yaml` 文件。
- `--execute` : 布尔开关；不接受额外参数；不传时只输出清理计划，传入后才真正写文件和删除脚手架。
- `--skip-audit` : 布尔开关；不接受额外参数；跳过清理前的 `#assert_no_temporary_axioms` 检查。
- `--skip-build` : 布尔开关；不接受额外参数；跳过清理后的 `lake build` 验证。
- `--keep-docs` : 布尔开关；不接受额外参数；保留 `docs/temporary_axiom.md`，并在宿主 README 使用了可移除 marker 时保留对应文档块。
- `--audit-script`、`--readme-file`、`--lakefile`、`--utility-module` : 这些是高级定制参数；接受路径或模块名字符串；主要用于把脚本迁移到非默认布局的宿主项目时覆写默认路径。

## 部署建议

建议把本仓库当作“工具源”来同步到宿主项目，而不是要求用户手工重命名模块前缀。

宿主项目只需：

1. 复制 `TemporaryAxiomTool/`、`approved_statement_registry_db/`、`scripts/`
2. 在宿主 `lakefile.toml` 里新增 `[[lean_lib]] name = "TemporaryAxiomTool"`
3. 在业务证明文件里 `import TemporaryAxiomTool.TemporaryAxiom`
4. 在 CI 或收尾阶段用 `run_temporary_axiom_audit.sh --module ...` 做模块级审计

这样可以最大程度降低接入成本与后续维护成本。
