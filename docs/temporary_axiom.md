# Temporary Axiom 说明文档

## 目标

`TemporaryAxiom` 用于并行形式化时的“定理到临时公理”转换，但转换必须满足两个前提：

1. 被跳过的定理陈述已经先行形式化，并进入已批准陈述表
2. 工作组只在确有并行依赖需要时，才显式添加 `@[temporary_axiom]`

因此，`@[temporary_axiom]` 仅用于在跳过非工作区已批准定理或跳过过于困难的已批准定理.

## 总体架构

方案由三层组成：

1. 外部注册库数据库：
   [approved_statement_registry_db/README.md](../approved_statement_registry_db/README.md)
2. 由 Python 工具生成的 Lean 注册表模块：
   [TestProject3/ApprovedStatementRegistry.lean](../TestProject3/ApprovedStatementRegistry.lean)
3. 临时公理转换与即时校验模块：
   [TestProject3/TemporaryAxiom.lean](../TestProject3/TemporaryAxiom.lean)

职责划分如下：

- 外部数据库负责版本管理、历史事件、评论、警报和回滚
- 生成的 Lean 注册表负责在编译期提供只读校验数据
- `TemporaryAxiom` 负责语法改写、`@[temporary_axiom]` 标签注册和即时合法性检查

## Lean 侧文件

[TestProject3/TemporaryAxiom.lean](../TestProject3/TemporaryAxiom.lean)：

- 定义 `@[temporary_axiom]`
- 将带标签的 theorem 改写为 axiom
- 在声明处立即校验是否存在批准记录以及 statement hash 是否匹配
- 提供 `#print_temporary_axioms` 与 `#assert_no_temporary_axioms` 作为审计

[TestProject3/ApprovedStatementRegistry.lean](../TestProject3/ApprovedStatementRegistry.lean)：

- 手写的注册库根模块
- 提供 statement hash 算法
- 提供给 Python 工具调用的 `#print_approved_statement_probe`
- 把生成的批准记录装配成 `approvedStatementMap`

[TestProject3/ApprovedStatementRegistry/Types.lean](../TestProject3/ApprovedStatementRegistry/Types.lean)：

- 定义结构体 `ApprovedStatement`
- `ApprovedStatementMap`
- 供自动生成分片复用的稳定类型接口

[TestProject3/ApprovedStatementRegistry/Generated.lean](../TestProject3/ApprovedStatementRegistry/Generated.lean)：

- 自动生成的汇总文件, 只负责把 `Shards/` 里的各分片的数组拼接成 `generatedApprovedStatements`
- import 所有 chapter/section 分片

[TestProject3/ApprovedStatementRegistry/Shards/](../TestProject3/ApprovedStatementRegistry/Shards/)：

- 自动生成的 chapter/section Lean 分片目录, `Shards/` 里每个文件保存一个 chapter/section 的实际条目
- 承载提供给 Lean 注册的批准记录数据

#### 编译行为与报错时机

对如下跳过定理的声明：

```lean
@[temporary_axiom]
theorem SectionMainTheorem_2 (h : (P ∧ Q) ∧ R) : R ∧ (Q ∧ P) := by
  {}
```

Lean 侧顺序如下：

1. 解析器读取完整 `declaration`
2. command macro 发现该 theorem 带有 `@[temporary_axiom]`
3. macro 丢弃 `:= by ...`，仅保留声明头，将其改写为 `axiom`
4. Lean 对改写后的声明进行正常 elaboration
5. `@[temporary_axiom]` 属性在 `afterTypeChecking` 阶段运行
6. 属性处理器读取已经导入环境的已批准陈述表，校验：
   - 定理名是否已批准
   - 当前 elaborated statement hash 是否与批准记录一致

注意:
- Lean 不会在 macro 节点直接查询外部 JSON 数据库
- 外部数据库交互发生在编译之前，由 Python 工具离线完成
- 非法 `@[temporary_axiom]` 会在该声明自身处立刻报错，不会等到后续引用时才失败

#### 转换不变量

macro 只做一件事：把 `theorem` 头改成 `axiom` 头。

它保留原始声明中的：

- 命名空间
- 声明名
- universe 参数
- binder 列表
- 返回类型
- `private`、`protected`、doc comment、其他属性

因此参数绑定、命名空间解析和 Lean 原生声明行为保持一致，避免转换造成参数或命名空间不一致。

#### import 边界

1. 普通工程文件：

- 不需要任何额外 import

2. 含有 `@[temporary_axiom]` 的工程文件：

- 只需要 `import TestProject3.TemporaryAxiom`

3. 工具文件:

[TestProject3/TemporaryAxiom.lean](../TestProject3/TemporaryAxiom.lean) 本身只依赖：
- `Lean`
- `TestProject3.ApprovedStatementRegistry`

[TestProject3/ApprovedStatementRegistry.lean](../TestProject3/ApprovedStatementRegistry.lean) 本身只依赖：
- `Lean`
- `TestProject3.ApprovedStatementRegistry.Types`
- `TestProject3.ApprovedStatementRegistry.Generated`

[TestProject3/ApprovedStatementRegistry/Generated.lean](../TestProject3/ApprovedStatementRegistry/Generated.lean) 只依赖：
- `TestProject3.ApprovedStatementRegistry.Types`
- `TestProject3.ApprovedStatementRegistry.Shards.*`

因此更新已批准陈述表时，只会改动方案内部控制的 Lean 文件与外部数据库文件，不会把 import 变化扩散到其他业务证明文件。

## 外部数据库

外部数据库位于：
- 当前状态：`approved_statement_registry_db/current/`
- 历史事件：`approved_statement_registry_db/history/`

当前实现中：

- `current/` 的 entry 不再重复保存 `chapter`、`section`、`shard_id`
- `statement_pretty` 以 UTF-8 可读字符直接保存，便于人工审阅
- `history/` 通过 `before_shard` / `after_shard` 保留回滚所需的定位信息

格式详见[外部数据库文档](../approved_statement_registry_db/README.md)


#### 管理工具

统一入口：

- [scripts/manage_approved_statement_registry.py](../scripts/manage_approved_statement_registry.py)

全局参数：

- `--project-root <PATH>`
- 可选参数
- 默认值为 `.`，表示 Lean 项目根目录
- 所有子命令都支持这个参数

参数格式约定：

- `<PATH>`
- 文件系统路径，例如 `.`、`/home/mouao/lean_projects/test_project3`
- `<MODULE_NAME>`
- Lean 模块名，例如 `TestProject3.Section2`
- `<DECL_NAME>`
- Lean 全限定声明名，例如 `TestProject3.SectionMainTheorem_2`
- `<NUM>`
- 十进制整数，例如 `0`、`2`
- `<EVENT_ID>`
- 历史事件编号，例如 `20260330T084637Z_approve_bbee340b`
- `--decl`
- 可重复参数；同一命令中写多次表示一次处理多个定理

支持的核心命令：

- `approve`
- `commit`
- `history`
- `rollback`
- `prune`
- `audit`
- `generate`

##### `approve`

用途：

- 将一个或多个已形式化且陈述可信的定理写入已批准陈述库
- 若该定理已经存在，则更新其记录
- 若其原先位于别的 chapter/section 分片，工具会自动迁移到新分片

必填参数：

- `--module <MODULE_NAME>`
- `--chapter <NUM>`
- `--section <NUM>`
- 至少一个 `--decl <DECL_NAME>`

可选参数：

- `--reason <TEXT>`: 默认值为 `approved statement freeze`
- `--author <TEXT>`: 默认值为 `ai-agent`
- `--skip-build`: 开关参数；提供后跳过 `lake build`

副作用：

- 调用 Lean probe 读取当前声明的 elaborated statement
- 更新 `approved_statement_registry_db/current/`
- 追加一条 `approve` 历史事件到 `approved_statement_registry_db/history/`
- 重建 Lean 侧 `Generated.lean` 与 `Shards/`
- 默认重新 build `TestProject3.ApprovedStatementRegistry`

典型 `approve` 示例：

```bash
python3 scripts/manage_approved_statement_registry.py approve \
  --module TestProject3.Section2 \
  --chapter 0 \
  --section 2 \
  --decl TestProject3.SectionMainTheorem_2
```

同一模块、同一小节下一次导入多个定理：

```bash
python3 scripts/manage_approved_statement_registry.py approve \
  --module TestProject3.Section2 \
  --chapter 0 \
  --section 2 \
  --decl TestProject3.lemma_2_1 \
  --decl TestProject3.lemma_2_2 \
  --decl TestProject3.SectionMainTheorem_2
```

##### `commit`

用途：

- 为已批准定理追加审核备注、风险提示或人工复核提醒

必填参数：

- 至少一个 `--decl <DECL_NAME>`
- `--message <TEXT>`

可选参数：

- `--severity <LEVEL>`: 可选值为 `comment`、`warning`、`alert`, 默认值为 `warning`
- `--reason <TEXT>`: 默认情况下写入 `--message` 的内容
- `--author <TEXT>`: 默认值为 `ai-agent`

副作用：

- 不改动 statement hash
- 更新对应条目的 `review_notes`、`review_status`、`needs_human_review`
- 追加一条 `commit` 历史事件

追加审核备注：

```bash
python3 scripts/manage_approved_statement_registry.py commit \
  --decl TestProject3.SectionMainTheorem_2 \
  --severity warning \
  --message "Check whether implicit arguments were inferred as intended."
```

##### `history`

用途：

- 查看注册库历史事件
- 可用于审计 agent 操作记录，也可配合 `rollback` 选择回退点

必填参数：

- 无

可选参数：

- `--decl <DECL_NAME>`: 可重复；提供后只显示涉及这些定理的事件
- `--limit <NUM>`: 最多输出多少条事件

输出内容：

- 事件编号、动作类型、作者、时间、原因
- 每条变更涉及的定理名和变更类型
- 若该事件新增了 review note，则额外打印该 note

查看历史：

```bash
python3 scripts/manage_approved_statement_registry.py history \
  --decl TestProject3.SectionMainTheorem_2
```

##### `rollback`

用途：

- 按历史事件编号回滚一次 `approve`、`commit`、`prune` 或更早的 `rollback`

必填参数：

- `--event-id <EVENT_ID>`

可选参数：

- `--reason <TEXT>`: 默认值为 `rollback of <EVENT_ID>`
- `--author <TEXT>`: 默认值为 `ai-agent`
- `--skip-build`: 开关参数；提供后跳过 `lake build`

副作用：

- 根据目标事件中的 `before/after` 记录反向重放修改
- 追加一条新的 `rollback` 历史事件
- 重建 Lean 侧注册表
- 默认重新 build

回滚事件：

```bash
python3 scripts/manage_approved_statement_registry.py rollback \
  --event-id <history-event-id>
```

##### `prune`

用途：

- 从已批准陈述库中删除一个或多个定理

必填参数：

- 至少一个 `--decl <DECL_NAME>`

可选参数：

- `--reason <TEXT>`: 默认值为 `removed from approved statement registry`
- `--author <TEXT>`: 默认值为 `ai-agent`
- `--skip-build`: 开关参数；提供后跳过 `lake build`

副作用：

- 从 `current/` 中删除对应条目
- 追加一条 `prune` 历史事件
- 重建 Lean 侧注册表
- 默认重新 build

注意：

- 若源码中某个 theorem 仍带有 `@[temporary_axiom]`，而它对应的批准记录被 `prune` 删除，则 Lean 会在该声明处立刻报错

示例：

```bash
python3 scripts/manage_approved_statement_registry.py prune \
  --decl TestProject3.SectionMainTheorem_2 \
  --reason "remove from approved registry"
```

##### `audit`

用途：

- 将当前 Lean 环境中的声明重新 probe，并与注册库中的 `statement_hash` 做比对
- 可选地把 review note 的严重级别提升为 CI 失败条件

必填参数：

- 无

可选参数：

- `--decl <DECL_NAME>`: 可重复；提供后只审计这些定理
- `--fail-on-review-status <LEVEL>`: 可选值为 `comment`、`warning`、`alert`
- 若选中条目的 `review_status` 达到或超过该级别，则命令失败

输出内容：

- hash 不一致时直接失败
- review note 存在时打印最近一条提示
- 无问题时打印通过统计

审计：

```bash
python3 scripts/manage_approved_statement_registry.py audit
```

按 review status 阈值触发失败：

```bash
python3 scripts/manage_approved_statement_registry.py audit \
  --fail-on-review-status warning
```

##### `generate`

用途：

- 根据 `approved_statement_registry_db/current/` 的当前快照重新生成 Lean 侧注册表文件
- 适合处理合并冲突后的重建，或只想刷新生成文件时使用

必填参数：

- 无

可选参数：

- `--skip-build`: 开关参数；提供后跳过 `lake build`

副作用：

- 重写 `TestProject3/ApprovedStatementRegistry/Generated.lean`
- 重写 `TestProject3/ApprovedStatementRegistry/Shards/`
- 默认重新 build

示例：

```bash
python3 scripts/manage_approved_statement_registry.py generate
```

## 并行工作流

推荐把整个流程分成四个阶段：

1. 上游先形式化并冻结可能需要依赖的定理陈述
2. 通过已批准陈述表登记这些可被跳过的陈述
3. 各 section 小组并行形式化，必要时才添加 `@[temporary_axiom]`
4. 证明补齐后移除标签，最终审计并清理脚手架

#### 预批准陈述

上游工作组先把可能成为依赖接口的定理陈述写对，再运行 `approve`。工具会：

1. 调用 Lean probe 读取当前声明
2. 自动生成 `decl_name`、`statement_hash`、`statement_pretty`
3. 写入外部数据库
4. 重建 Lean 侧已批准陈述表分片

如果某个陈述虽然先登记了，但仍希望人工重点审核，可追加 `commit` 备注。支持的备注级别：

- `comment`
- `warning`
- `alert`

#### 并行形式化

只有在确实需要跳过某个已批准定理时，才在对应 theorem 上加入：

```lean
@[temporary_axiom]
theorem SectionMainTheorem_2 (h : (P ∧ Q) ∧ R) : R ∧ (Q ∧ P) := by
  {}
```

如果该定理未被批准，或当前 theorem header 与批准陈述不一致，Lean 会在这一行声明处立即报错。

#### 合并与恢复

并行开始前：

1. 上游分支先合并“陈述冻结 + 已批准陈述表更新”
2. 主集成分支开启已批准陈述表审计
3. 各 section 分支再按需添加 `@[temporary_axiom]`

并行进行中：

- 小组只改自己 section 的证明文件
- 若新增可跳过接口，先更新已批准陈述表，再提交标签变更
- `approved_statement_registry_db/history/` 持续记录 approve、commit、prune、rollback 事件

恢复真实证明时：

1. theorem owner 补全证明
2. 删除 `@[temporary_axiom]`
3. 合并恢复提交
4. 如某条批准记录已无必要，可后续再 `prune`

冲突处理建议：

- `approved_statement_registry_db/current/` 与 `history/` 冲突时，不手工拼接 JSON
- 先保留双方事件文件
- 再运行 `python3 scripts/manage_approved_statement_registry.py generate`
- 必要时重新执行 `audit`

## 审计与 CI/CD

推荐把审计分成两类。

常开审计：

- 已批准陈述表一致性审计

闭环审计：

- 临时公理清零审计

#### 已批准陈述表审计

统一入口：

- [scripts/run_approved_statement_registry_audit.sh](../scripts/run_approved_statement_registry_audit.sh)

GitHub Actions 片段：

```yaml
      # approved-statement-registry-audit:start
      - name: Approved statement registry audit
        run: ./scripts/run_approved_statement_registry_audit.sh
      # approved-statement-registry-audit:end
```

这一步适合长期启用，因为它只检查：

- 当前 Lean 声明是否仍与批准记录一致
- 当前日志中是否存在 review note
- 如果使用 `--fail-on-review-status`，还能把高风险陈述升级为 CI 失败

#### 临时公理清零审计

当某一集成分支声明“已经没有任何 `@[temporary_axiom]`”时，再启用：

```yaml
      # temporary-axiom-audit:start
      - name: Temporary axiom closure audit
        run: ./scripts/run_temporary_axiom_audit.sh
      # temporary-axiom-audit:end
```

入口文件：

- [TemporaryAxiomAudit.lean](../TemporaryAxiomAudit.lean)

只要环境里还存在任何临时公理，这个审计就会失败，因此不适合在并行开发早期常开。

## 清理工作流

当项目已经不再需要此脚手架工具时：

1. 删除所有 `@[temporary_axiom]`
2. 确认 `lake env lean TemporaryAxiomAudit.lean` 通过
3. 运行 dry-run：

```bash
python3 scripts/cleanup_temporary_axiom_scaffolding.py
```

4. 确认输出后执行：

```bash
python3 scripts/cleanup_temporary_axiom_scaffolding.py --execute
```

清理脚本会一并移除：

- `TemporaryAxiom` 模块及相关 import
- `ApprovedStatementRegistry` Lean 模块与生成分片
- `approved_statement_registry_db/`
- 审计脚本
- CI/CD 中用标记包裹的审计步骤
- 相关文档

## 安装与迁移

当需要迁移此工具时建议至少复制以下内容。

必须迁移的 Lean 文件：

- [TestProject3/TemporaryAxiom.lean](../TestProject3/TemporaryAxiom.lean)
- [TestProject3/ApprovedStatementRegistry.lean](../TestProject3/ApprovedStatementRegistry.lean)
- [TestProject3/ApprovedStatementRegistry/Types.lean](../TestProject3/ApprovedStatementRegistry/Types.lean)

初始化时需要准备的自动生成 Lean 产物：

- [TestProject3/ApprovedStatementRegistry/Generated.lean](../TestProject3/ApprovedStatementRegistry/Generated.lean)
- [TestProject3/ApprovedStatementRegistry/Shards/](../TestProject3/ApprovedStatementRegistry/Shards/)

必须迁移的脚本与外部数据库：

- [scripts/manage_approved_statement_registry.py](../scripts/manage_approved_statement_registry.py)
- [approved_statement_registry_db/README.md](../approved_statement_registry_db/README.md)

建议一并迁移的辅助文件：

- [TemporaryAxiomAudit.lean](../TemporaryAxiomAudit.lean)
- [scripts/run_approved_statement_registry_audit.sh](../scripts/run_approved_statement_registry_audit.sh)
- [scripts/run_temporary_axiom_audit.sh](../scripts/run_temporary_axiom_audit.sh)
- [scripts/cleanup_temporary_axiom_scaffolding.py](../scripts/cleanup_temporary_axiom_scaffolding.py)

目标项目至少需要准备：

```text
YourProject/
  YourProject/TemporaryAxiom.lean
  YourProject/ApprovedStatementRegistry.lean
  YourProject/ApprovedStatementRegistry/Types.lean
  YourProject/ApprovedStatementRegistry/Generated.lean
  YourProject/ApprovedStatementRegistry/Shards/
  approved_statement_registry_db/current/
  approved_statement_registry_db/history/
  scripts/manage_approved_statement_registry.py
```

迁移步骤：

1. 复制 Lean 文件到目标项目命名空间下
2. 把 `TestProject3` 模块前缀统一替换成你的项目名
3. 检查 Lean 文件中的 `import`、`namespace`
4. 修改 Python 脚本中的项目常量：
   - Lean 根目录
   - build target
5. 初始化空的 `Generated.lean`、分片目录和外部数据库目录
6. 在目标项目中需要临时跳过定理的文件里加入：

```lean
import YourProject.TemporaryAxiom
```

建议初始 `Generated.lean`：

```lean
/- Auto-generated registry aggregate. Do not edit by hand. -/
import YourProject.ApprovedStatementRegistry.Types

namespace YourProject.ApprovedStatementRegistry

def generatedApprovedStatements : Array ApprovedStatement := #[]

end YourProject.ApprovedStatementRegistry
```

建议首次自检：

```bash
lake build
```

```bash
python3 scripts/manage_approved_statement_registry.py generate --skip-build
```

```bash
python3 scripts/manage_approved_statement_registry.py audit
```

如果迁移了闭环审计文件，再运行：

```bash
lake env lean TemporaryAxiomAudit.lean
```
