# TemporaryAxiomTool

`TemporaryAxiomTool` 是一个 Lean 4 proof-session 准备器。它面向“单次 attempt + 本地持久 proved theorem 数据库”的工作流。

仓库里的文档分工：

- `README.md`
  快速使用和术语总览。
- `docs/temporary_axiom.md`
  当前实现的技术规格。
- `docs/downstream_upgrade_note.md`
  从旧版 `PreparedSession` / managed-attr 工作流迁移到当前架构时需要注意的变化。

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

如果模块产物缺失，或 `.ilean` / `.trace` 已经过期，`prepare` 会尽早报错。确需让工具补构建时，可显式给：

```bash
python3 scripts/temporary_axiom_session.py prepare \
  --target YourProject.Section:goal \
  --auto-build
```

`prepare` 默认会在最后真实重建一次 target module，确认 prepared workspace 可直接编译。若只想先生成 session，再把这轮验证留给后续 `lake build`，可显式给：

```bash
python3 scripts/temporary_axiom_session.py prepare \
  --target YourProject.Section:goal \
  --no-verify
```

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
2. 预检 target 相关模块和发生变化的 tracked modules，确认 `.ilean` / `.olean` / `.trace` 与当前源码一致；必要时可配合 `--auto-build`。
3. 对发生变化或被标记 dirty 的 tracked modules 做“全量维护”：
   先丢弃这些模块旧的 proved entries，再重新扫描并登记当前已证明 theorem / lemma。
4. 扫描 target closure，收集本次 session-local 的显式 `sorry` theorem / lemma。
5. 生成 `TemporaryAxiomTool/TheoremRegistry/Shards/**/*.lean`。
6. 必要时给 tracked modules 插入稳定 shard import。
7. 默认重建一次 target module 作为 verify；`--no-verify` 可跳过。
8. 写出 `session.json` 与明文报告。

`prepare` 里的冻结信息统一使用 Lean elaboration 后的 theorem-side statement hash，而不是源码文本 hash。

## `cleanup` 会做什么

`cleanup` 不再回滚源码 import，也不再删除 shard 文件。它只做三件事：

1. 对本次发生变化的 tracked modules 做增量扫描，把新证明成功的 theorem / lemma 加进 proved DB。
2. 把所有 shard 重写为“无活动 session”的 inactive 状态。
3. 删除 `.temporary_axiom_session/session.json` 和 `temporary_axiom_tool_session_report.txt`。

这意味着：

- target theorem 如果在本次 session 中被证明完成，也会在 `cleanup` 时被增量登记进 proved DB。
- 下一次 `prepare` 时，之前已经登记过的 proved theorem 会自动进入 permitted 集合，无需再依赖标签。

## `session.json` 里有什么

`.temporary_axiom_session/session.json` 只保留稳定的 `freeze` 数据，供外层工具读取。核心字段：

- `target`
  `decl_name` 是 Lean 真实全限定声明名；`module` 是定义模块名；`statement_hash` 是 theorem-side hash。
- `tracked_modules`
  当前被 theorem registry 跟踪的项目模块列表。
- `module_closure`
  为本次 target 收集 session-temporary 定理时考察过的项目内模块闭包。
- `session_temporary_axioms`
  本次 session-local 的显式 `sorry` theorem / lemma。
- `permitted_axioms`
  当前 session 最终允许被公理化的声明集合，等于 `persistent proved ∪ session temporary - {target}`。

## 产物

当前版本会在仓库里维护这些文件：

- `.temporary_axiom_session/session.json`
- `temporary_axiom_tool_session_report.txt`
- `.temporary_axiom_registry/proved_theorems.json`
- `TemporaryAxiomTool/TheoremRegistry/Shards/**/*.lean`

## 文档

- [docs/temporary_axiom.md](docs/temporary_axiom.md)
- [docs/downstream_upgrade_note.md](docs/downstream_upgrade_note.md)
