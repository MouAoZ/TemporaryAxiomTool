# 下游项目升级说明

这份说明面向已经把 `TemporaryAxiomTool` 同步进宿主项目、现在要从旧版本升级到当前版本的维护者。

当前版本的重点不是新增某个单点命令，而是把整个工具整理成了更适合 Lean 4 module system 的结构，同时收紧了 registry 数据模型和 CI 入口。因此升级时应把它看成一次“整包替换 + 数据保留 + 全链路回归测试”，而不是只替换某几个 Lean 文件。

相关文档：

- 版本变化与外部引用变化见 [update_record.md](./update_record.md)
- 完整技术规格、工具行为与命令参数说明见 [temporary_axiom.md](./temporary_axiom.md)
- 数据库 schema 与字段语义见 [../approved_statement_registry_db/README.md](../approved_statement_registry_db/README.md)

## 本次升级的影响范围

需要一起更新的部分有：

- Lean 侧库文件
  - `TemporaryAxiomTool.lean`
  - `TemporaryAxiomTool/`
- 管理脚本
  - `scripts/manage_approved_statement_registry.py`
  - `scripts/registry_tool/`
- 说明文档
  - `docs/update_record.md`
  - `docs/temporary_axiom.md`
  - `approved_statement_registry_db/README.md`
- CI 命令
  - 旧版若还在调用 `./scripts/run_approved_statement_registry_audit.sh`，必须改成当前的 Python 入口

需要保留、不要被上游空模板覆盖的部分有：

- `approved_statement_registry_db/current/`
- `approved_statement_registry_db/history/`

原因很简单：当前版本已经把 ordinary helper、meta extension 与 metaprogramming 入口重新分层，生成的 Lean shard 文件也改成了 `module` 头与 `public import` / `public def` 形式；只同步其中一半，最容易得到“构建能过一部分、但在宿主项目或 CI 中随机报错”的状态。

## 升级前准备

建议先做三件事：

1. 在宿主项目开单独分支，或先做一次完整备份。
2. 备份当前数据库目录：
   - `approved_statement_registry_db/current/`
   - `approved_statement_registry_db/history/`
3. 记录当前 CI 中与本工具相关的命令，准备一起替换。

如果你的下游项目以前用过中间过渡版本，先检查 `approved_statement_registry_db/current/` 中的 JSON 是否已经是当前格式。当前版本期望的人工审核字段是：

- `status`
- `commit`

如果数据库仍在使用更早期的字段名或条目结构，请先按 [../approved_statement_registry_db/README.md](../approved_statement_registry_db/README.md) 的当前格式整理，再运行下面的生成与审计命令。当前版本不提供旧 schema 的专门兼容层。

## 推荐升级步骤

### 1. 整包同步工具代码

不要只拷贝单个 Lean 文件。最稳妥的做法是同步整个工具目录集合：

- `TemporaryAxiomTool.lean`
- `TemporaryAxiomTool/`
- `scripts/manage_approved_statement_registry.py`
- `scripts/registry_tool/`
- `docs/temporary_axiom.md`
- `docs/update_record.md`
- `approved_statement_registry_db/README.md`

如果你的宿主项目把工具作为 vendored code 直接放在仓库根目录，这一步通常就是直接覆盖这些工具代码与文档文件，但保留你自己的 `approved_statement_registry_db/current/` 和 `history/` 数据。

### 2. 更新 CI 入口

旧版 CI 如果仍写着：

```yaml
- name: Approved statement registry audit
  run: ./scripts/run_approved_statement_registry_audit.sh
```

现在应改成：

```yaml
- name: Approved statement registry audit
  run: python3 scripts/manage_approved_statement_registry.py audit
```

如果还要检查仓库中是否剩余 `@[temporary_axiom]`，加入：

```yaml
- name: Temporary axiom audit
  run: |
    python3 scripts/manage_approved_statement_registry.py \
      audit-temporary-axioms \
      --module YourProject
```

### 3. 重新生成 Lean 侧 registry 文件

同步完成后，先不要直接信任旧的生成物。先运行：

```bash
python3 scripts/manage_approved_statement_registry.py generate
```

### 4. 先构建工具本体，再构建宿主项目

```bash
lake build TemporaryAxiomTool
lake build
```

第一条命令确认工具本身与 generated registry 可以单独通过；第二条命令确认宿主项目整体导入链没有被这次升级打断。

### 5. 重新跑 registry 审计

```bash
python3 scripts/manage_approved_statement_registry.py audit
python3 scripts/manage_approved_statement_registry.py audit-temporary-axioms \
  --module YourProject
```

如果你希望把人工标记状态也当成发布阻断条件，可再跑：

```bash
python3 scripts/manage_approved_statement_registry.py audit --fail-on-status needs_attention
```

更完整的命令语义和参数行为仍以 [temporary_axiom.md](./temporary_axiom.md) 为准；这份说明只保留升级时必须知道的调用路径和回归测试点。

## 上线前的周全测试清单

下面这组测试更偏保守，但适合在正式生产环境前做一次完整排雷。

### A. Python 侧基础检查

```bash
python3 -m py_compile scripts/manage_approved_statement_registry.py scripts/registry_tool/*.py
```

作用：

- 及早发现同步时漏文件、缩进损坏、解释器版本问题

### B. 生成与构建检查

```bash
python3 scripts/manage_approved_statement_registry.py generate
lake build TemporaryAxiomTool
lake build
```

通过标准：

- `generate` 成功完成
- `TemporaryAxiomTool` 单独可构建
- 宿主项目整体可构建

### C. 当前 registry 一致性审计

```bash
python3 scripts/manage_approved_statement_registry.py audit
```

可选地，如果你希望把人工标记状态也当成发布阻断条件，可再跑：

```bash
python3 scripts/manage_approved_statement_registry.py audit --fail-on-status needs_attention
```

### D. 临时公理闭包审计

```bash
python3 scripts/manage_approved_statement_registry.py audit-temporary-axioms \
  --module YourProject
```

如果你还在开发阶段允许存在临时公理，这个命令至少要确认它能正确运行；如果你准备做发布候选或正式交付，则应把它跑到完全通过。

### E. `approve` 冒烟测试

这一步会修改注册库，建议只在临时分支或测试副本中做。

先挑一个 disposable theorem，或新建一个只用于升级验证的测试声明。然后运行：

```bash
python3 scripts/manage_approved_statement_registry.py approve \
  --module YourProject.Section1 \
  --chapter 1 \
  --section 1 \
  --decl YourProject.Section1.someTheorem
```

通过后建议立刻检查：

```bash
python3 scripts/manage_approved_statement_registry.py report --all --verbose --lifecycle
```

确认点：

- 条目被写入正确 shard
- `statement_pretty`、`statement_hash` 正常
- 生命周期字段更新合理

### F. `@[temporary_axiom]` 真实链路测试

还是在临时分支或测试副本中进行：

1. 先让一个测试 theorem 以正常证明形式存在并通过构建。
2. 对该 theorem 运行一次 `approve`。
3. 把它改成 `@[temporary_axiom] theorem ... := by ...`。
4. 重新 `lake build`，确认当前 registry 能放行。

这一步实际覆盖的是：

- theorem -> axiom 改写
- attribute 校验
- generated registry 查找
- statement hash 比对

### G. statement 变更回归测试

这一步是最重要的升级回归项，建议至少做一次。

在一个已经批准的测试 theorem 上，故意做一个会改变 elaborated type 的改动，例如：

- 改 binder 顺序
- 改 binder implicitness
- 改返回类型或依赖的常量

然后验证三件事：

1. 不重新 `approve` 时，`@[temporary_axiom]` 校验会失败。
2. 重新 `approve` 后，条目重新通过。
3. `history` 中新增了一条记录。

检查命令：

```bash
python3 scripts/manage_approved_statement_registry.py history \
  --decl YourProject.Section1.someTheorem \
  --verbose
```

同时再跑一次：

```bash
python3 scripts/manage_approved_statement_registry.py report \
  --decl YourProject.Section1.someTheorem \
  --verbose \
  --lifecycle
```

预期行为：

- 如果 `statement_hash` 改变，旧 `commit` 会被清空
- `status` 会自动重置为 `needs_attention`
- 只有这类 hash 变化会写入 `history/`

### H. CI 路径测试

在正式合并前，至少让一条临时 PR 或测试分支完整跑过一次 CI，确认：

- 不再引用已删除的 shell 脚本
- registry audit 能正常执行
- 如已配置，temporary axiom audit 也能正常执行

## 常见失败症状与优先排查方向

### 症状 1

```text
Invalid definition `...initFn✝`, may not access declaration `...` marked as `meta`
```

优先排查：

- 是否只更新了部分 Lean 文件，漏掉了新增的 `Runtime.lean` 或 `Hash.lean`
- 是否宿主项目中还残留旧版生成文件
- 是否忘了在同步后重新运行 `generate`

### 症状 2

```text
./scripts/run_approved_statement_registry_audit.sh: No such file or directory
```

优先排查：

- CI 是否还在调用旧 shell 脚本
- 是否已经改成 `python3 scripts/manage_approved_statement_registry.py audit`

### 症状 3

```text
`--module` 不允许重复值：e, t, r, ...
```

优先排查：

- 下游项目是否只更新了入口脚本，但没同步最新的 `scripts/registry_tool/cli.py`

### 症状 4

```text
approved statement registry 审计失败：
```

优先排查：

- theorem statement 是否真的发生了语义变化
- 当前 `approved_statement_registry_db/current/` 是否还是旧快照
- 是否在升级过程中手工编辑过 JSON 但没有重新 `generate`

## 发布与回滚建议

建议的发布顺序是：

1. 在升级分支完成整包同步与全部回归测试。
2. 确认 `generate`、`lake build TemporaryAxiomTool`、`lake build`、`audit`、CI 都通过。
3. 再把这次工具升级合并进正式开发分支。

如果需要回滚，最稳妥的做法是一起回滚：

- `TemporaryAxiomTool.lean`
- `TemporaryAxiomTool/`
- `scripts/manage_approved_statement_registry.py`
- `scripts/registry_tool/`
- 与升级前对应的数据备份

不要只回滚其中一半；对这个工具来说，Lean 侧模块结构、生成器和 registry 数据格式本来就是一组联动约束。
