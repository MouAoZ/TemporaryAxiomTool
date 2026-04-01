# TemporaryAxiomTool 技术规格与工作流说明

相关文档：

- 快速接入与最小使用方式见 [../README.md](../README.md)
- 数据库 schema 与字段语义见 [../approved_statement_registry_db/README.md](../approved_statement_registry_db/README.md)
- 当前版本的结构变化与 breaking changes 见 [../CHANGELOG.md](../CHANGELOG.md)
- 下游升级步骤与测试清单见 [downstream_upgrade_note.md](./downstream_upgrade_note.md)

## 目标

`TemporaryAxiomTool` 用于并行形式化中的“定理陈述先批准，证明后补齐”工作流。

它解决的问题是：

1. 某个 theorem 的 statement 已经足够可信，允许下游先依赖它
2. 证明体暂时缺失，但希望在 Lean 中显式、可审计地跳过
3. 审核者需要快速接手当前批准陈述的人工确认工作
4. 最终仍需要通过审计确保全部临时公理被清除

它不解决的问题是：

- 让任意 theorem 都能静默跳过证明
- 用外部 JSON 直接影响 Lean 编译结果
- 代替人工数学审阅

## 术语

- 已批准陈述: 已写入 `approved_statement_registry_db/current/` 的 theorem statement 快照
- current 快照: 当前仍然有效的批准陈述集合，是活动注册库的唯一真相来源
- status: 审核者对条目当前可信度的显式判断，取值为 `safe`、`needs_attention`、`unreliable`
- commit: 审核者留下的人工评论条目；默认覆盖，显式 `--append` 才增量追加
- history: 仅记录 statement hash 变化的历史目录，不记录 `commit/status` 变更
- statement hash: 对 theorem elaborated type 计算得到的稳定哈希值，用于比较陈述是否漂移

## 总体架构

工具由五个部分组成：

1. Lean 侧临时公理工具:
   [../TemporaryAxiomTool/TemporaryAxiom.lean](../TemporaryAxiomTool/TemporaryAxiom.lean)
2. Lean 侧临时公理 runtime 模块:
   [../TemporaryAxiomTool/TemporaryAxiom/Runtime.lean](../TemporaryAxiomTool/TemporaryAxiom/Runtime.lean)
3. Lean 侧批准注册表:
   [../TemporaryAxiomTool/ApprovedStatementRegistry.lean](../TemporaryAxiomTool/ApprovedStatementRegistry.lean)
4. Lean 侧 statement hash 模块:
   [../TemporaryAxiomTool/ApprovedStatementRegistry/Hash.lean](../TemporaryAxiomTool/ApprovedStatementRegistry/Hash.lean)
5. 外部注册库数据库:
   [../approved_statement_registry_db/README.md](../approved_statement_registry_db/README.md)

职责划分：

- `TemporaryAxiom` 负责 theorem -> axiom 改写、属性校验和最终审计命令
- `TemporaryAxiom.Runtime` 负责 `temporary_axiom` 的 meta extension 与环境查询
- `ApprovedStatementRegistry` 负责离线 probe 命令和运行时查找表装配
- `ApprovedStatementRegistry.Hash` 负责 statement hash 计算
- 外部数据库负责 current 快照、人工审核元数据与 statement 版本历史

## 仓库依赖

当前工具源仓库本体只要求 Lean toolchain：

- `leanprover/lean4:v4.29.0-rc8`

当前实现没有额外依赖 `mathlib4`、`checkdecls` 或 `repl`。

对宿主项目还有一个额外前提：

- 当前版本要求宿主项目已经采用 Lean module system
- 旧式非 `module` 业务模块不在当前支持范围内

如果宿主项目本身依赖这些包，应由宿主项目自己的 `lakefile.toml` 负责声明；工具不会替宿主项目引入它们。

## Lean 侧 ordinary/meta 分层

当前 Lean 实现已经按 module system 友好的方式拆分：

- ordinary 模块
  - [../TemporaryAxiomTool/ApprovedStatementRegistry/Hash.lean](../TemporaryAxiomTool/ApprovedStatementRegistry/Hash.lean)
- metaprogramming 入口模块
  - [../TemporaryAxiomTool/TemporaryAxiom/Runtime.lean](../TemporaryAxiomTool/TemporaryAxiom/Runtime.lean)
  - [../TemporaryAxiomTool/ApprovedStatementRegistry.lean](../TemporaryAxiomTool/ApprovedStatementRegistry.lean)
  - [../TemporaryAxiomTool/TemporaryAxiom.lean](../TemporaryAxiomTool/TemporaryAxiom.lean)

拆分原则：

- `elab`、`macro_rules`、attribute 注册与 `temporary_axiom` extension 放进 compile-time 模块
- statement hash 与 registry 类型等 ordinary helper 放进独立 ordinary 模块
- 生成出来的 Lean shard/aggregate 文件也使用 `module` 头，避免宿主项目切换 module system 后出现 phase 或可见性问题

## Lean 侧语义规格

### `@[temporary_axiom]` 的处理流程

当 Lean 读到：

```lean
@[temporary_axiom]
theorem YourProject.someTheorem (h : P ∧ Q) : Q ∧ P := by
  sorry
```

处理顺序如下：

1. parser 读入完整 `declaration`
2. `declaration` kind 上的 `macro_rules` 检测该 theorem 是否带有 `@[temporary_axiom]`
3. 宏丢弃证明体，仅保留声明头，并将 `theorem` 改写为 `axiom`
4. Lean 对改写后的声明正常 elaboration
5. attribute 在 `afterTypeChecking` 阶段运行
6. 工具读取当前环境中已经导入的批准注册表，检查：
   - 声明名是否已被批准
   - 当前 elaborated statement hash 是否与批准记录一致

因此：

- 下游模块看到的是一个真正进入环境的 `axiom`
- 非法标签会在声明处立即报错，而不是等到下游使用时才失败
- 运行时只认批准 registry，不认外部 JSON 原始文件
- `temporary_axiom` 保留显式 attribute syntax，并在 import 阶段完成 attribute 注册

### Lean 运行时实际依赖哪些数据

Lean 编译时不会直接读取外部 JSON 数据库。

运行时只消费由脚本生成的 Lean 模块：

- [../TemporaryAxiomTool/ApprovedStatementRegistry/Generated.lean](../TemporaryAxiomTool/ApprovedStatementRegistry/Generated.lean)
- `TemporaryAxiomTool/ApprovedStatementRegistry/Shards/` 下的自动生成分片

这些生成文件当前都使用 `module` 头，并由脚本输出 `public import` / `public def` 形式的共享声明。

同样地，`approve`、`audit`、`audit-temporary-axioms` 在执行时生成的临时 probe / 审计文件也使用 `module` 头，因此它们要求宿主业务模块本身也是 module-system 文件。

当前实现中，Lean 运行时真正依赖的批准条目字段只有：

- `decl_name`
- `statement_hash`
- `shard_id`

像 `status`、`commit`、`statement_pretty`、`approved_by` 这些字段只服务于离线管理、审计和人工复核，不参与 Lean 运行时判定。

### statement hash 的语义边界

工具比较的不是源代码表面文本，而是 theorem 最终 elaborated type 的哈希值。

原则上：

- 如果最终 elaborated type 相同，则 hash 相同
- 如果最终 elaborated type 改变，则 hash 改变

下面这些改动通常不会改变 hash：

- 仅重命名 binder 名
- 只改 pretty-print、换行、注释或 JSON 元数据
- 只改 notation，但 elaboration 后得到完全相同的类型表达式

下面这些改动通常会改变 hash：

- 改 theorem statement 的 domain、codomain 或中间依赖
- 改 binder 顺序
- 改 binder implicitness，例如显式参数改成隐式参数
- 改 universe 结构
- 改 namespace 解析或常量解析，导致 elaborated type 指向不同常量

对使用者而言，最安全的理解方式是：

- 不要猜测“这算不算小改动”
- 只要 theorem header 的语义可能变了，就重新运行 `approve`

如果宿主项目是在从旧式显式 namespace 文件迁移到 `module` 文件，除了 statement hash 之外，还要额外核对声明全名是否漂移；一旦 `decl_name` 变化，旧 registry 条目也需要重新校正。

## import 规则

宿主项目普通文件不需要额外 import。

只有需要使用下列功能的文件才需要：

- `@[temporary_axiom]`
- `#print_temporary_axioms`
- `#assert_no_temporary_axioms`

对应导入：

```lean
import TemporaryAxiomTool.TemporaryAxiom
```

## 当前对象模型

对单个 declaration 而言，当前工具只关心三件事：

- 是否已批准
  - 未批准: 不在 current 快照中，不能使用 `@[temporary_axiom]`
  - 已批准: 在 current 快照中，可以被 registry 校验
- 当前审核状态
  - `safe`
  - `needs_attention`
  - `unreliable`
- 当前人工评论
  - `commit = []`
  - `commit = [ ... ]`

注意：

- `status` 与 `commit` 互不推导
- 可以只改 `status` 不写 `commit`
- 也可以只写 `commit`，保持 `status = safe`
- `approve` 如果发现 `statement_hash` 改变，会清空旧 `commit` 并把 `status` 设为 `needs_attention`

## 管理脚本的定位

统一入口：

- [../scripts/manage_approved_statement_registry.py](../scripts/manage_approved_statement_registry.py)

脚本会根据它自身所在路径自动定位项目根目录，因此正常使用时不需要指定额外的项目根路径参数。

正常工作流默认面向人工使用，而不是面向外部程序调用：

- 报表和 history 只提供文本输出
- `approve`、`prune`、`generate` 会自动重建 Lean 侧 registry 目标
- 临时 probe / 审计文件始终自动删除
- `approve` / `audit` 使用的临时 probe 文件会在项目根目录下生成合法模块文件名
- `audit-temporary-axioms` 生成的临时审计文件也带 `module` 头

## 命令行为规格

### 通用 CLI 约定

- 文中的 `decl_name` / `--decl` 都指 Lean 全限定声明名
- 文中的 `module` / `--module` 都指 Lean import 路径，不是文件系统路径
- 支持重复传入的列表参数都不接受重复值

### `approve`

作用：

- 通过 Lean 离线 probe 当前 declaration 的 elaborated type
- 把 statement 快照写入指定 chapter/section shard
- 重建 Lean 侧 generated registry 文件并重新构建 registry 目标

状态变化：

- 若 declaration 之前不存在于 current 中，则新增条目
- 若 declaration 已存在，则原条目会被覆盖更新
- 若 declaration 原本位于其他 shard，会先从旧 shard 移除，再写入新 shard
- 若 `statement_hash` 未变化，则保留原有 `status` 与 `commit`
- 若 `statement_hash` 变化，则清空原有 `commit`，并把 `status` 重置为 `needs_attention`
- 只有 `statement_hash` 变化时才写入 `history/`

参数说明：

- `--module`: 用于探测声明的 Lean 模块；取值为 Lean import 路径；单次命令只接受一个模块，且该模块必须能导入本次所有 `--decl`
- `--chapter`: 注册库分片所属 chapter 编号；十进制整数
- `--section`: 注册库分片所属 section 编号；十进制整数
- `--decl`: 要批准的定理名；支持重复传入多个不同声明
- `--reason`: 审批原因；默认 `approved statement freeze`
- `--author`: 最近一次批准的操作者；默认 `ai-agent`

示例：

```bash
python3 scripts/manage_approved_statement_registry.py approve \
  --module YourProject.Section2 \
  --chapter 3 \
  --section 2 \
  --decl YourProject.someTheorem
```

批量批准多个不同声明：

```bash
python3 scripts/manage_approved_statement_registry.py approve \
  --module YourProject.Section2 \
  --chapter 3 \
  --section 2 \
  --decl YourProject.someTheorem \
  --decl YourProject.anotherTheorem
```

### `commit`

作用：

- 更新一个或多个已批准条目的 `status`
- 覆盖、追加或清理 `commit` 条目

状态变化：

- 不改变 `statement_hash`
- 不写任何 `history/` 文件
- 会更新 current 快照中的人工审核元数据

参数说明：

- `--decl`: 已批准定理名；支持重复传入多个不同声明
- `--status`: 显式设置审核状态；取值为 `safe`、`needs_attention`、`unreliable`
- `--message`: 要写入的人工评论；默认覆盖该条目的整个 `commit` 列表
- `--append`: 追加一条 `commit` 而不是覆盖；必须和 `--message` 一起使用
- `--clear`: 清空该条目的全部 `commit`
- `--drop`: 删除指定序号的 `commit`；取值为 1-based 正整数
- `--author`: 写入 `commit` 元数据的操作者；默认 `ai-agent`

常见用法：

覆盖式提交：

```bash
python3 scripts/manage_approved_statement_registry.py commit \
  --decl YourProject.someTheorem \
  --status needs_attention \
  --message "binder order changed; manual review suggested"
```

增量追加：

```bash
python3 scripts/manage_approved_statement_registry.py commit \
  --decl YourProject.someTheorem \
  --append \
  --message "secondary reviewer confirmed issue scope"
```

仅改状态：

```bash
python3 scripts/manage_approved_statement_registry.py commit \
  --decl YourProject.someTheorem \
  --status unreliable
```

清空全部 `commit`：

```bash
python3 scripts/manage_approved_statement_registry.py commit \
  --decl YourProject.someTheorem \
  --clear
```

删除第 2 条 `commit`：

```bash
python3 scripts/manage_approved_statement_registry.py commit \
  --decl YourProject.someTheorem \
  --drop 2
```

### `report`

作用：

- 为人工审核者快速打印当前条目

默认行为：

- 在不额外传入 `--all`、`--decl`、`--status` 时，只打印 `commit` 非空的条目
- 默认字段为 `decl_name`、`status`、`statement_pretty`、全部 `commit`

参数说明：

- `--decl`: 精确声明名过滤；支持重复传入多个不同声明
- `--status`: 状态过滤；取值为 `safe`、`needs_attention`、`unreliable`，支持重复传入
- `--all`: 打印全部 current 条目，而不是默认的“仅有 commit 的条目”
- `--verbose`: 追加打印 `module`、`shard`、`approval_reason`
- `--lifecycle`: 再追加打印生命周期元数据

示例：

```bash
python3 scripts/manage_approved_statement_registry.py report
```

```bash
python3 scripts/manage_approved_statement_registry.py report \
  --status needs_attention \
  --verbose \
  --lifecycle
```

### `audit`

作用：

- 重新 probe current 中的声明
- 比较注册库里的 `statement_hash` 与当前 Lean 环境中的真实 hash
- 若存在 `status != safe` 或 `commit` 非空的条目，会额外打印摘要提示，便于人工复核

参数说明：

- `--decl`: 审计范围过滤；支持重复传入多个不同声明；省略时审计 current 中全部声明
- `--fail-on-status`: 审核状态阈值；取值为 `needs_attention`、`unreliable`；若任一被选中条目的 `status` 达到或超过该级别则命令失败

示例：

```bash
python3 scripts/manage_approved_statement_registry.py audit
```

```bash
python3 scripts/manage_approved_statement_registry.py audit --fail-on-status needs_attention
```

### `audit-temporary-axioms`

作用：

- 临时生成一个 Lean 审计入口文件
- 导入 `TemporaryAxiomTool.TemporaryAxiom`
- 导入你通过 `--module` 传入的宿主模块
- 执行 `#assert_no_temporary_axioms`
- 审计结束后自动删除临时文件

参数说明：

- `--module`: 导入到临时审计入口中的 Lean 模块；至少需要提供一个，支持重复传入多个不同模块

示例：

```bash
python3 scripts/manage_approved_statement_registry.py audit-temporary-axioms \
  --module YourProject
```

### `prune`

作用：

- 从 current 快照中移除一个或多个声明
- 重建 Lean 侧 generated registry 文件并重新构建 registry 目标

参数说明：

- `--decl`: 要移除的定理名；支持重复传入多个不同声明

示例：

```bash
python3 scripts/manage_approved_statement_registry.py prune \
  --decl YourProject.someTheorem
```

### `history`

作用：

- 查看 statement hash 变化历史

注意：

- `history/` 只记录 statement hash 变化
- 改 `status` 或 `commit` 不会写入 `history/`

参数说明：

- `--decl`: 声明名过滤；支持重复传入多个不同声明
- `--limit`: 输出条数上限；十进制整数
- `--verbose`: 打印 shard 与 before/after statement

示例：

```bash
python3 scripts/manage_approved_statement_registry.py history
```

```bash
python3 scripts/manage_approved_statement_registry.py history \
  --decl YourProject.someTheorem \
  --verbose
```

### `generate`

作用：

- 仅根据 `approved_statement_registry_db/current/` 重写 Lean 侧生成文件
- 随后重新构建 registry 目标

适用场景：

- 当前快照是对的，但生成出来的 Lean 文件丢了或不同步
- 你手工修复了 current 中的 JSON，需要重新生成 Lean 侧 registry

示例：

```bash
python3 scripts/manage_approved_statement_registry.py generate
```

## 审计与 CI/CD

建议在宿主项目 CI 中至少加入两个检查：

1. registry hash 审计
2. temporary axiom 闭包审计

例如：

```yaml
- name: Approved statement registry audit
  run: python3 scripts/manage_approved_statement_registry.py audit

- name: Temporary axiom audit
  run: |
    python3 scripts/manage_approved_statement_registry.py \
      audit-temporary-axioms \
      --module YourProject
```

## 最终清理建议

当前版本不再提供自动 cleanup 脚本。

推荐的人工退场流程是：

1. 运行 `audit-temporary-axioms`，确认已经没有 `@[temporary_axiom]`
2. 删除业务文件中的 `import TemporaryAxiomTool...`
3. 从宿主 `lakefile.toml` 中移除 `TemporaryAxiomTool` 对应 `lean_lib`
4. 删除同步进去的 `TemporaryAxiomTool/`、`approved_statement_registry_db/` 与 `scripts/`
5. 重新 `lake build` 验证宿主项目已经脱离工具依赖
