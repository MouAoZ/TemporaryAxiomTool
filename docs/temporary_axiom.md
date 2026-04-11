# TemporaryAxiomTool 使用手册

这份文档描述工具的用户可见行为：如何接入、如何运行 `prepare` / `cleanup`、会生成哪些文件、外层工具应读取哪些信息，以及常见错误该如何处理。

## 1. 工具做什么

`TemporaryAxiomTool` 面向“单次证明尝试 + 本地持久 proved theorem 数据库”的工作流。

一次活动 session 中，工具会把当前环境里的顶层 `theorem` / `lemma` 分成四类：

1. `target`
   本次要证明的目标定理。它继续按普通 `theorem` elaboration，不跳过证明体。
2. `persistent proved`
   来自本地 proved theorem 数据库的已证明定理。校验 theorem-side statement hash 后，直接作为同名公理注册。
3. `session temporary`
   本次 target closure 中显式 `sorry` 的 theorem / lemma。校验 theorem-side statement hash 后，也作为同名公理注册。
4. `other`
   既不是 target，也不在 permitted 集合中的 theorem / lemma。保持普通 Lean 行为。

工具统一使用 theorem-side statement hash。也就是说，校验基于 Lean elaboration 后的声明头，而不是源码文本。

## 2. 接入项目

### 2.1 tracked module

只有直接 `import TemporaryAxiomTool` 的项目模块才是 tracked module。

这条规则决定了三件事：

- 哪些模块可以作为 `prepare --target` 的定义模块
- 哪些模块会被维护到 proved theorem 数据库
- 哪些模块会保留稳定 shard import

如果某个模块需要被工具跟踪，只需要在源码里加入：

```lean
import TemporaryAxiomTool
```

### 2.2 stable shard import

第一次 `prepare` 后，工具会给每个 tracked module 补齐一条稳定 import：

```lean
import TemporaryAxiomTool.TheoremRegistry.Shards.<Module>
```

这条 import 之后会一直保留在源码里。`cleanup` 不会删除它。

### 2.3 transient shard import

如果 target closure 里某个未 tracked 的普通模块命中了本次 session 的显式 `sorry` theorem / lemma，`prepare` 会临时给它插入对应 shard import。`cleanup` 会删除这类临时 import。

## 3. 命令行

工具入口是：

```bash
python3 scripts/temporary_axiom_session.py
```

它有两个子命令：

- `prepare`
- `cleanup`

### 3.1 `prepare`

常见用法：

```bash
python3 scripts/temporary_axiom_session.py prepare --target MyProj.Mod:goal
python3 scripts/temporary_axiom_session.py prepare --target MyProj.Mod:My.Namespace.goal
python3 scripts/temporary_axiom_session.py prepare --target MyProj.Mod.goal
python3 scripts/temporary_axiom_session.py prepare --target MyProj.Mod:goal --auto-build
```

参数：

- `--target`
  支持两种格式：
  - `<module>:<decl>`
  - `<fully-qualified-decl>`
- `--auto-build`
  允许工具在目标解析或产物检查时自动补构建缺失或过期的模块产物
- `--base-commit`
  可选备注。若提供，会写入 `session.json`；否则默认记录当前 `HEAD`

### 3.2 `--target` 的写法

推荐在已知定义模块时使用：

```bash
--target MyProj.Mod:goal
```

这里的规则是：

- `MyProj.Mod` 必须是目标声明的定义模块
- `goal` 若不含 `.`，按该模块中的短名匹配
- `goal` 若含 `.`，按 Lean 全限定声明名匹配，并要求它确实定义在给定模块里

也可以直接写完整声明名：

```bash
--target MyProj.Namespace.goal
```

这种写法适合外层工具已经拿到完整声明名，但不知道定义模块的场景。

### 3.3 `prepare` 会做什么

`prepare` 的用户可见流程是：

1. 检查当前没有活动 session。
2. 解析 target，并确认目标模块已经纳入 tracked modules。
3. 检查是否存在残留的 transient shard import。
4. 必要时给 tracked modules 补齐稳定 shard import。
5. 刷新 changed tracked modules 的 proved theorem 数据库。
6. 收集本次 target closure 中的 session temporary axioms。
7. 写入 active shards。
8. 若 target module 本轮还没有在 collect 阶段被真实构建过，再额外做一次 `lake build <target-module>` 验证 prepared workspace。
9. 写出 session 文件和报告。

若第 1 步发现仓库里已经有活动 session，`prepare` 会直接退出，现有 session 文件和报告保持不变。

执行成功后，终端会打印：

- target module
- tracked modules 数量
- target closure 大小
- session temporary axioms 数量
- total permitted axioms 数量
- `session.json` 路径
- report 路径
- proved DB 路径

### 3.4 `cleanup`

命令：

```bash
python3 scripts/temporary_axiom_session.py cleanup
```

`cleanup` 会做三件事：

1. 把当前 session 涉及到的 shard 写回 inactive 状态
2. 删除 prepare 临时插入到 untracked 模块里的 shard import
3. 删除当前活动 session 的文件

`cleanup` 不会改动 proved theorem 数据库。

## 4. 活动 session 中的运行时行为

### 4.1 target

target theorem 会：

- 按 theorem header 规则 elaboration 出最终声明名和类型
- 计算 theorem-side statement hash
- 与 `session.json` 里的冻结值比对
- 校验通过后继续普通 theorem elaboration

### 4.2 permitted theorems

permitted theorem 包括：

- `persistent proved`
- `session temporary`

它们都会：

- 先做 theorem-side statement hash 校验
- 校验通过后直接注册为同名公理
- 跳过证明体 elaboration

### 4.3 其他定理

不在 target 或 permitted 集合中的 theorem / lemma，不受工具影响，保持普通 Lean 行为。

### 4.4 `@[temporary_axiom]`

显式 `@[temporary_axiom]` 仍然可用，它是一个可选的显式校验入口。

用户只需要记住两点：

- 它不是脚本自动插入的 managed 标记
- 它只有在活动 session 中才有意义

## 5. 输出文件

### 5.1 `.temporary_axiom_session/session.json`

这是外层工具应读取的主文件。

结构示例：

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

关键字段：

- `freeze.target`
  当前目标定理的真实 `decl_name`、定义模块 `module`、`statement_hash`
- `freeze.tracked_modules`
  当前 tracked modules 列表
- `freeze.module_closure`
  本次 target 的模块闭包
- `freeze.session_temporary_axioms`
  本次 session-local temporary axioms
- `freeze.permitted_axioms`
  当前 session 完整 permitted 集合，带 `module`、`statement_hash`、`origin`
- `freeze.permitted_axioms_list`
  仅含声明名的 permitted 数组，可直接给 comparator 使用
- `transient_shard_import_modules`
  本次 prepare 临时插入 shard import、cleanup 需要删除的 untracked 模块列表

如果外层工具需要“完整 permitted 集合加 target”，应读取：

- `freeze.permitted_axioms_list`
- `freeze.target.decl_name`

### 5.2 `temporary_axiom_tool_session_report.txt`

这是给人读的摘要，包含：

- target
- tracked module 列表
- target closure
- permitted axioms 按模块分组后的列表
- comparator 可直接复用的 `permitted_axioms_list`
- 相关产物路径

### 5.3 `.temporary_axiom_registry/proved_theorems.json`

这是本地持久 proved theorem 数据库。

结构示例：

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

其中：

- `tracked_modules`
  上次维护时看到的 tracked modules
- `module_digests`
  用于判断 tracked modules 源码是否变化
- `proved_theorems`
  已登记的持久 proved theorem / lemma

### 5.4 generated shards

工具会维护：

```text
TemporaryAxiomTool/TheoremRegistry/Shards/**/*.lean
```

这是运行时使用的 per-module shard 源码。

## 6. 推荐工作流

### 6.1 普通用户

1. 在要跟踪的模块里直接 `import TemporaryAxiomTool`
2. 运行 `prepare --target ...`
3. 让 proving agent 或人工继续工作
4. 外层 verifier / comparator 读取 `.temporary_axiom_session/session.json`
5. 运行 `cleanup`

### 6.2 外层工具

如果你在写调度器，建议这样接：

1. 调 `prepare`
2. 读取 `.temporary_axiom_session/session.json`
3. 使用：
   - `freeze.target`
   - `freeze.permitted_axioms_list`
   - 如有需要再读 `freeze.permitted_axioms`
4. 在会话结束后调 `cleanup`

## 7. 常见错误

所有用户态错误都使用统一格式：

```text
TemporaryAxiomTool 错误：...

详情：
- ...

建议：
- ...
```

### 7.1 `target 参数格式无效`

说明 `--target` 不是这两种格式之一：

- `<module>:<decl>`
- `<fully-qualified-decl>`

优先改成：

```bash
--target MyProj.Mod:goal
```

### 7.2 `目标模块尚未纳入 theorem registry 跟踪`

说明目标定义模块没有直接：

```lean
import TemporaryAxiomTool
```

先把目标模块接入 tracked 集合，再重试。

### 7.3 `prepare 需要与当前源码一致的模块产物`

说明工具在解析目标或检查依赖时发现 `.ilean` / `.olean` / `.trace` 缺失或与当前源码不一致。

处理方式：

- 手动运行 `lake build`
- 或重试 `prepare --auto-build`

### 7.4 `已有活动 session`

说明当前仓库里已经存在 `.temporary_axiom_session/session.json`。

处理方式：

- 继续当前会话
- 或先执行 `cleanup`

这类报错不会清除已有 session；`session.json` 和报告会保留，直到用户显式运行 `cleanup`。

### 7.5 `另一个 prepare 正在运行`

说明锁文件 `.temporary_axiom_session/prepare.lock` 已存在。

处理方式：

- 等待当前 `prepare` 结束
- 若上一次异常退出，确认无活跃进程后删除锁文件

### 7.6 `检测到无活动 session 下残留的 transient shard import`

说明上一次 `prepare` / `cleanup` 中断后，某个未 tracked 模块里残留了临时 shard import。

处理方式：

- 按报错列表删除对应模块源码里的临时 shard import
- 然后重新运行 `prepare`

### 7.7 `当前没有可清理的活动 session`

说明当前没有 `cleanup` 可处理的活动 session。

处理方式：

- 先运行 `prepare`
- 或确认上一次 session 已经 cleanup 完成

## 8. 最小自检

```bash
python3 -m py_compile \
  scripts/temporary_axiom_session.py \
  scripts/session_tool/*.py

lake build TemporaryAxiomTool

python3 scripts/temporary_axiom_session.py prepare \
  --target TemporaryAxiomTool.TestFixture.Target:goal
lake build TemporaryAxiomTool.TestFixture.Target
python3 scripts/temporary_axiom_session.py cleanup
lake build TemporaryAxiomTool.TestFixture.Target
```
