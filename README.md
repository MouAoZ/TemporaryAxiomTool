# TemporaryAxiomTool

`TemporaryAxiomTool` 是一个 Lean 4 proof-session 准备器。它面向“单次 attempt + 本地持久 proved theorem 数据库”的工作流。

仓库里的文档分工：

- `README.md`
  快速使用和术语总览。
- `docs/temporary_axiom.md`
  当前实现的技术规格。
- `docs/downstream_upgrade_note.md`
  从旧版 `PreparedSession` / managed-attr 工作流迁移到当前架构时需要注意的变化。

## `wild-skip` 相对 `main`

当前分支文档描述的是 `wild-skip`，不是 `main`。

相对 `main`，这个分支的主要区别只有一处流程优化：

- `main`
  `prepare` 会对 `changed tracked ∪ target closure 内的 tracked modules` 一起做 collect build。
- `wild-skip`
  若 `target closure` 里的 tracked modules 都没变，则只对真正变了的 tracked modules 做 collect build；对未变化但与当前 target 相关的 tracked modules，改为本地 collect replay 取 theorem-side hash。
- 两者共同点
  都保留 theorem-side hash 语义，也都只在必要时做最后一次真实 `lake build <target-module>` verify。`wild-skip` 改的是 steady-state prepare 的成本，不改安全边界。

## 快速使用

先构建工具：

```bash
lake build TemporaryAxiomTool
```

准备一次 session：

```bash
python3 scripts/temporary_axiom_session.py prepare \
  --target YourProject.Section:goal
```

也可以直接传完整声明名：

```bash
python3 scripts/temporary_axiom_session.py prepare \
  --target YourProject.Section.goal
```

若用 `<module>:<decl>` 形式，`prepare` 会直接通过 collect build 收集 tracked modules 的 theorem-side hash。只有在按完整声明名解析 target、或需要补齐某些普通模块产物时，才会用到 `--auto-build`：

```bash
python3 scripts/temporary_axiom_session.py prepare \
  --target YourProject.Section:goal \
  --auto-build
```

`prepare` 现在只在必要时才会做最后一次真实 `lake build <target-module>`：

- 若 target module 没有在本轮 collect build 中真实跑过，仍会做 eager active verify。
- 若 target module 已在本轮 collect build 中真实跑过，则跳过这次 eager verify。

结束后清理当前 session：

```bash
python3 scripts/temporary_axiom_session.py cleanup
```

外层工具应读取 `.temporary_axiom_session/session.json`，再取其中的 `freeze` 字段。

```python
session = json.load(open(".temporary_axiom_session/session.json"))
freeze = session["freeze"]
```

## 当前工作流

只有直接 `import TemporaryAxiomTool` 的项目模块才会被跟踪。对这些 tracked modules，工具维护两类状态：

- 单次 session 状态
  写在 `.temporary_axiom_session/session.json` 和 `temporary_axiom_tool_session_report.txt`。
- 本地持久 proved theorem 数据库
  写在 `.temporary_axiom_registry/proved_theorems.json`。

同时，每个 tracked module 都会对应一个稳定 shard import：

- `import TemporaryAxiomTool.TheoremRegistry.Shards.<Module>`

这个 import 只会在第一次需要时插入一次，之后保留在源码里。`cleanup` 不再删除它；`cleanup` 只会把 shard 写回“无活动 session”的状态。

若 target closure 里有未直接 `import TemporaryAxiomTool` 的普通模块，但其中命中了本次 session 的显式 `sorry` theorem / lemma，`prepare` 也会临时给这些模块插入对应 shard import。这个更改只在当前 session 期间存在，`cleanup` 会删掉它。

## 现在会追踪哪几类定理

活动 session 中，工具把定理分成四类：

- `target`
  本次要证明的目标定理。它仍按 `theorem` 正常 elaboration，但 theorem-side statement hash 必须和冻结值一致。
- `persistent proved`
  来自 `.temporary_axiom_registry/proved_theorems.json` 的已证明定理。活动 session 中会先检查 theorem-side hash，再直接注册为同名 `axiom`，跳过证明体。
- `session temporary`
  本次 target closure 里显式 `sorry` 的 theorem / lemma。活动 session 中也会先检查 theorem-side hash，再注册为同名 `axiom`。
- `other`
  不在上述集合中的 theorem / lemma。保持正常 elaboration，不会被自动改写。

显式 `@[temporary_axiom]` 仍然可用，但不再由脚本自动插入。它只是一种显式声明“这里应当属于 permitted set”的校验入口；没有活动 session 时，它会直接报错。

## `prepare` 会做什么

`prepare` 的主流程是：

1. 解析 target，要求目标模块已经纳入 tracked modules。
2. 先做一轮便宜的早期检查；若检测到无活动 session 下残留的 transient shard import，会立即报错，不再继续长流程。
3. 必要时给 tracked modules 插入稳定 shard import。
4. 找出发生变化的 tracked modules，并取 `changed tracked ∪ target closure 内的 tracked modules`。
5. 把这些模块切到 collect 模式，逐模块真实 `lake build`，一次拿到 theorem-side hash、显式 `sorry` 标记和源码顺序信息。
6. 用 collect 结果刷新持久 proved DB：只登记非 `sorry` 的 theorem / lemma。
7. 从 collect 结果中提取 tracked 部分的 session-local temporary theorems；对 target closure 里未 tracked 的模块，先做源码扫描找候选，再临时插入 shard import，用本地 collect replay 取得正确的 theorem-side hash。
8. 写入 active shards。
9. 只有在必要时才做一次 eager active verify；否则直接跳过。
10. 写出 `session.json` 与明文报告。

`prepare` 里的冻结信息统一使用 Lean elaboration 后的 theorem-side statement hash，而不是源码文本 hash。

## `cleanup` 会做什么

`cleanup` 现在只做清理和模式转换：

1. 把当前 session 涉及到的 host modules 写回 inactive shards。
2. 删除 prepare 曾临时插入到 untracked session-temporary 模块里的 shard import。
3. 删掉 `.temporary_axiom_session/session.json` 和 `temporary_axiom_tool_session_report.txt`。

这意味着：

- `cleanup` 不再刷新 proved DB。
- tracked modules 若在活动 session 期间发生变化，会在下一次 `prepare` 时由 collect 阶段统一刷新 proved DB。

## `session.json` 里有什么

`.temporary_axiom_session/session.json` 只保留稳定的 `freeze` 数据，供外层工具读取。核心字段：

- `target`
  `decl_name` 是 Lean 环境里的真实声明名；它可能带 namespace，也可能就是裸短名。`module` 是定义模块名；`statement_hash` 是 theorem-side hash。
- `tracked_modules`
  当前被 theorem registry 跟踪的项目模块列表。
- `module_closure`
  为本次 target 收集 session-temporary 定理时考察过的项目内模块闭包。
- `session_temporary_axioms`
  本次 session-local 的显式 `sorry` theorem / lemma。
- `permitted_axioms`
  当前 session 最终允许被公理化的声明集合，等于 `persistent proved ∪ session temporary - {target}`。
- `permitted_axioms_list`
  只含声明名的长列表，格式直接对应 comparator 需要的 permitted-axiom 字符串数组。

此外，`session.json` 顶层还会记录：

- `transient_shard_import_modules`
  本次 `prepare` 临时插入 shard import、并应在 `cleanup` 时删掉的 untracked 模块列表。

## 产物

当前版本会在仓库里维护这些文件：

- `.temporary_axiom_session/session.json`
- `temporary_axiom_tool_session_report.txt`
- `.temporary_axiom_registry/proved_theorems.json`
- `TemporaryAxiomTool/TheoremRegistry/Shards/**/*.lean`

## 文档

- [docs/temporary_axiom.md](docs/temporary_axiom.md)
- [docs/downstream_upgrade_note.md](docs/downstream_upgrade_note.md)
