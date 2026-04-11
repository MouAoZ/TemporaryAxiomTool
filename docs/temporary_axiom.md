# TemporaryAxiomTool 技术规格

这份文档描述当前 `wild-skip` 架构的实现行为。

相关文档：

- 快速使用见 [../README.md](../README.md)
- 迁移说明见 [downstream_upgrade_note.md](./downstream_upgrade_note.md)

## `wild-skip` 相对 `main`

这份文档描述的是当前 `wild-skip` 分支，不是 `main`。

两者的核心语义保持一致：

- 都使用 theorem-side statement hash。
- 都在活动 session 中把 permitted theorem 改写成同名 axiom。
- 都保留“必要时才执行”的最终真实 `lake build <target-module>` verify。

`wild-skip` 相对 `main` 的主要实现差异是 `prepare` 的 steady-state 优化：

- `main`
  每次 `prepare` 都会把 `changed tracked ∪ target closure 内的 tracked modules` 一起切到 collect 模式并真实 `lake build`。
- `wild-skip`
  只有当 `target closure` 里存在已变化的 tracked module 时，才退回上述完整 collect 路径。
- `wild-skip`
  若 `target closure` 内的 tracked modules 都未变化，则只对真正变更的 tracked modules 做 collect build；target closure 内其余相关 tracked modules 改用本地 collect replay，只为当前 target 重新拿 theorem-side hash 与 `sorry` 信息。

因此，这个分支优化的是“第二次及后续 prepare”的成本，而不是放松校验。

## 1. 文件结构

```text
.
├── README.md
├── docs/
│   ├── temporary_axiom.md
│   └── downstream_upgrade_note.md
├── scripts/
│   ├── temporary_axiom_session.py
│   └── session_tool/
│       ├── cli.py
│       ├── common.py
│       └── lean_ops.py
├── TemporaryAxiomTool.lean
├── TemporaryAxiomTool/
│   ├── TemporaryAxiom.lean
│   ├── TheoremRegistry.lean
│   ├── TheoremRegistry/
│   │   ├── Types.lean
│   │   └── Shards/                       # 生成物：每个 tracked module 一份
│   ├── StatementHash.lean
│   └── TestFixture/
├── .temporary_axiom_session/
│   ├── prepare.lock                      # `prepare` 执行期间的互斥锁
│   └── session.json                      # 当前活动 session 的 freeze 数据
├── .temporary_axiom_registry/
│   └── proved_theorems.json              # 本地持久 proved theorem 数据库
└── temporary_axiom_tool_session_report.txt
```

说明：

- `TemporaryAxiomTool/TheoremRegistry/Shards/**/*.lean` 是工具管理的 runtime shard。tracked module 一旦接入，就会稳定 import 自己那份 shard。
- `.temporary_axiom_session/` 只在活动 session 期间存在。
- `.temporary_axiom_registry/proved_theorems.json` 是本地持久状态，不随 `cleanup` 删除。

## 2. 跟踪范围

只有直接 `import TemporaryAxiomTool` 的项目模块才会被视为 tracked module。

这条规则同时决定：

- 哪些模块允许作为 `prepare --target` 的定义模块。
- 哪些模块会被持续维护 proved theorem 数据库。
- 哪些模块源码里会保留稳定 shard import。

`prepare` 不会自动把整个项目都纳入跟踪。若目标模块尚未直接 import `TemporaryAxiomTool`，会直接报错。

## 3. 运行时中的四类定理

活动 session 中，`TemporaryAxiomTool.TemporaryAxiom` 只拦截顶层 `theorem` / `lemma` 声明，并按下面四类处理：

1. `target`
   目标定理。先按 theorem header 规则 elaboration 出最终声明名和类型，计算 theorem-side statement hash；校验通过后继续正常 theorem elaboration，不跳过证明体。
2. `persistent proved`
   来自 proved DB 的已证明定理。校验 theorem-side hash 后，直接注册为同名 `axiom`，跳过证明体 elaboration。
3. `session temporary`
   本次 target closure 里显式 `sorry` 的 theorem / lemma。处理方式与 `persistent proved` 相同，也是“先验 hash，再注册同名 axiom”。
4. `other`
   不在以上集合中的定理。完全不改写，走 Lean 内建 theorem elaboration。

无活动 session 时，命令 elaborator 不会做 theorem -> axiom 改写。

## 4. Statement Hash 语义

当前版本统一使用 theorem-side statement hash。

具体做法是：

1. 先按 theorem header 规则解析声明名、universe params、binders 和 type。
2. 对 elaboration 后的 `(levelParams, type)` 计算 hash。
3. 只对 target / permitted theorem 做校验。
4. 校验通过后，target 继续作为 theorem；permitted theorem 改写成同名 axiom。

这里不依赖源码文本，也不依赖 axiom-side replay。

## 5. `@[temporary_axiom]`

显式 `@[temporary_axiom]` 仍然存在，但语义变成：

- 它是一个显式校验入口，而不是脚本自动插入的 managed 标记。
- 只有活动 session 中才有意义。
- 目标定理不能带这个标签。
- 非 permitted 声明带这个标签会直接报错。
- permitted 声明即使不带这个标签，也会在活动 session 中自动按规则处理。

也就是说，标签现在是可选的；工具的主要机制已经不再依赖源码批量打标签。

## 6. `prepare`

命令行：

```bash
python3 scripts/temporary_axiom_session.py prepare --target MyProj.Mod:goal
python3 scripts/temporary_axiom_session.py prepare --target MyProj.Mod:My.Namespace.goal
python3 scripts/temporary_axiom_session.py prepare --target MyProj.Mod.goal
python3 scripts/temporary_axiom_session.py prepare --target MyProj.Mod:goal --auto-build
```

参数：

- `--target`
  支持 `<module>:<decl>` 或 `<fully-qualified-decl>`。
- `--auto-build`
  允许在目标解析或普通模块 probe 需要时，自动 `lake build` 缺失或过期的模块闭包。

### 6.1 target 解析

- `<module>:<decl>`
  这里的模块部分必须是声明的定义模块。
- 若 `decl` 不含 `.`
  按“定义模块里的短名”唯一匹配。
- 若 `decl` 含 `.`
  当成 Lean 全限定声明名处理，并要求它就在给定定义模块里。
- `<fully-qualified-decl>`
  只按声明名前缀尝试有限个候选模块；若声明名和模块路径不对齐，应改用前一种形式。

### 6.2 目标解析与构建前提

- 若使用 `<module>:<decl>`，`prepare` 不再要求 tracked modules 先有最新 `.ilean` / `.trace`；这些模块会直接进入 collect build。
- 若使用 `<fully-qualified-decl>`，仍需要依赖已有产物来解析候选模块中的目标声明；此时若候选模块产物缺失或过期，默认报错，显式给 `--auto-build` 才会自动补构建。
- 对 target closure 里未 tracked 的普通模块，session-temporary 收集改为“源码扫描找候选 + 临时插入 shard import + 本地 collect replay”，不再依赖 import-probe 取 theorem-side hash。
- 若检测到“没有直接 `import TemporaryAxiomTool` 的普通模块，却残留了自己的 shard import”，`prepare` 会在这里直接报错。这通常说明上一次 prepare / cleanup 中断，避免后面跑很久才发现环境本来就是脏的。

### 6.3 collect 与 persistent proved registry 对齐

`prepare` 会读取 `.temporary_axiom_registry/proved_theorems.json`，并取：

- 发生变化或被强制刷新的 tracked modules
- target closure 内的 tracked modules

随后把这些模块切到 collect 模式并逐模块真实 `lake build`。collect 阶段对每个 theorem / lemma 一次性得到：

- `decl_name`
- `statement_hash`
- `explicit_sorry`
- `ordinal`

其中：

- 非 `sorry` 的 theorem / lemma 会写入或刷新持久 proved DB。
- 显式 `sorry` 的 theorem / lemma 不会进入持久 proved DB。
- target module 的 `ordinal` 只用于决定哪些显式 `sorry` 能进入本次 session-temporary 集合。

### 6.4 session-local temporary axioms

在 proved DB 刷新完后，`prepare` 会针对 target 收集 session-local temporary axioms：

- 对 target closure 内的 tracked modules，直接复用 collect 结果。
- 对 target closure 内未 tracked 的模块，先做源码扫描找显式 `sorry` 候选，再临时插入 shard import，用本地 collect replay 收集 theorem-side hash。
- 依赖模块里：收集显式 `sorry` 的 theorem / lemma。
- target 所在模块里：只排除 target 自身以及排在 target 之后的显式 `sorry` theorem / lemma；排在 target 之后但已经证明完成的 theorem 仍可作为 persistent proved 生效。

### 6.5 permitted 集合

最终 permitted 集合为：

`persistent proved ∪ session temporary - {target}`

每个 permitted entry 都记录：

- `decl_name`
- `module`
- `statement_hash`
- `origin`

其中 `origin` 当前有两种值：

- `persistent_proved`
- `session_temporary`

### 6.6 shard 生成

工具会为每个 tracked module 生成一份 shard：

- 路径：
  `TemporaryAxiomTool/TheoremRegistry/Shards/<ModulePath>.lean`
- shard 内注册的数据：
  - 当前 host module
  - 当前 session 的 target name / hash
  - 该 host module 自己的 permitted theorem 表

注册使用 `#register_temporary_axiom_module_shard ...` 自定义命令，一次性把该模块的数据放进 `SimplePersistentEnvExtension`。

shard 有三种模式：

- `inactive`
- `collect`
- `active`

当前实现不是“每个 theorem 一条 marker declaration”，而是“每个模块一张表 + 一个模式位”。

### 6.7 稳定 import

如果 tracked module 还没有：

```lean
import TemporaryAxiomTool.TheoremRegistry.Shards.<Module>
```

`prepare` 会在 import 头里插入这行。这个 import 之后会一直保留；`cleanup` 不会删掉它。

若 target closure 里某个未 tracked 的普通模块命中了本次 session-temporary theorem，`prepare` 还会临时给该模块插入：

```lean
import TemporaryAxiomTool.TheoremRegistry.Shards.<Module>
```

这个 import 只在当前活动 session 期间保留；`cleanup` 会删掉它。

### 6.8 verify

`prepare` 的最终 verify 现在是条件触发：

```bash
lake build <target-module>
```

触发条件：

- 若 target module 没有在本轮 collect build 中真实构建过，则会执行这次 eager active verify。
- 若 target module 已在本轮 collect build 中真实构建过，则跳过这次 eager verify。

跳过的理由是：这时 target module 已经在 prepare 内真实过了一次 collect build，脚本只是在 collect 结果基础上把已知 theorem-side hash 写回 active runtime，不再需要立刻再跑一轮等价的 eager verify。

## 7. `cleanup`

命令行：

```bash
python3 scripts/temporary_axiom_session.py cleanup
```

`cleanup` 的行为：

1. 读取活动 session。
2. 把当前 session host modules 的 shard 重写为 inactive 状态：
   - `mode = inactive`
   - 不携带 target / permitted 数据
3. 删除 prepare 临时插入到 untracked session-temporary 模块里的 shard import。
4. 删除：
   - `.temporary_axiom_session/session.json`
   - `temporary_axiom_tool_session_report.txt`

`cleanup` 不再做下面这些旧行为：

- 不删除 tracked modules 的稳定 shard import
- 不回滚源码 attribute
- 不删除 `.temporary_axiom_registry/proved_theorems.json`
- 不在 cleanup 时刷新 proved DB

## 8. `session.json`

当前 `session.json` 结构：

```json
{
  "schema_version": 5,
  "base_commit": "optional git sha",
  "freeze": {
    "target": {
      "decl_name": "MyProj.Namespace.goal",
      "module": "MyProj.Mod",
      "statement_hash": "123"
    },
    "tracked_modules": ["MyProj.Mod", "MyProj.Dep"],
    "module_closure": ["MyProj.Mod", "MyProj.Dep"],
    "session_temporary_axioms": [
      {
        "decl_name": "MyProj.Dep.dep_sorry",
        "module": "MyProj.Dep",
        "statement_hash": "456",
        "origin": "session_temporary"
      }
    ],
    "permitted_axioms_list": [
      "MyProj.Dep.dep_done"
    ],
    "permitted_axioms": [
      {
        "decl_name": "MyProj.Dep.dep_done",
        "module": "MyProj.Dep",
        "statement_hash": "789",
        "origin": "persistent_proved"
      }
    ]
  },
  "transient_shard_import_modules": [
    "MyProj.OtherDep"
  ]
}
```

注意：

- 当前版本已经没有 `cleanup.edits`。
- `target.decl_name` 是 Lean 环境里的真实声明名；它可能带 namespace，也可能就是裸短名。
- `target.module` 永远是定义模块名。
- `freeze.permitted_axioms_list` 只保留声明名，格式直接对应 comparator 需要的 permitted-axiom 名字数组。
- `transient_shard_import_modules` 记录的是 prepare 临时插入 shard import、cleanup 需要删掉的 untracked 模块。

## 9. `proved_theorems.json`

proved DB 结构：

```json
{
  "schema_version": 2,
  "tracked_modules": ["MyProj.Mod", "MyProj.Dep"],
  "dirty_modules": [],
  "module_digests": {
    "MyProj.Mod": "deadbeef..."
  },
  "proved_theorems": [
    {
      "decl_name": "MyProj.Mod.helper",
      "module": "MyProj.Mod",
      "file": "MyProj/Mod.lean",
      "statement_hash": "123"
    }
  ]
}
```

字段语义：

- `tracked_modules`
  上次维护时看到的 tracked module 列表。
- `dirty_modules`
  兼容字段。当前实现把它当作“下一次 prepare 必须强制 collect 刷新”的模块集合；成功刷新后通常为空。
- `module_digests`
  用于判断 tracked module 源码是否变化。
- `proved_theorems`
  持久登记的已证明 theorem / lemma 列表。

## 10. 报告文件

`temporary_axiom_tool_session_report.txt` 是给人读的摘要，包含：

- target
- tracked module 列表
- target closure
- session temporary axioms 数量
- permitted axioms 按模块分组后的列表
- comparator 可直接复用的 `permitted_axioms_list`
- 本次 session 相关产物路径

它不是运行时输入；运行时只依赖 shard 和 `session.json`。

## 11. 实现边界

- 不解析 `.olean` 二进制格式。
- `.ilean` 只用于 declaration range 和 import 信息。
- 源码扫描只负责便宜地判定“theorem / lemma 是否显式 `sorry`”。
- tracked modules 的 theorem-side hash 通过 collect build 一次性收集；未 tracked 的 session-temporary 候选走“临时 shard import + 本地 collect replay”。
- 无活动 session 时，不进行 theorem -> axiom 改写。

## 12. 最小回归测试

```bash
python3 -m py_compile \
  scripts/temporary_axiom_session.py \
  scripts/session_tool/*.py

lake build TemporaryAxiomTool TemporaryAxiomTool.TestFixture.PrivateReplay

python3 scripts/temporary_axiom_session.py prepare \
  --target TemporaryAxiomTool.TestFixture.Target:goal
lake build TemporaryAxiomTool.TestFixture.Target
python3 scripts/temporary_axiom_session.py cleanup
lake build TemporaryAxiomTool.TestFixture.Target

python3 scripts/temporary_axiom_session.py prepare \
  --target TemporaryAxiomTool.TestFixture.PrivateReplay:goal
lake build TemporaryAxiomTool.TestFixture.PrivateReplay
python3 scripts/temporary_axiom_session.py cleanup
lake build TemporaryAxiomTool.TestFixture.PrivateReplay
```
