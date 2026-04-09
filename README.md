# TemporaryAxiomTool

`TemporaryAxiomTool` 是一个轻量的 Lean 4 session 准备器，用于单次证明 attempt。

仓库里的文档分工如下：

- `README.md`
  入口文档，只保留快速使用和文档导航。
- `docs/temporary_axiom.md`
  技术规格，说明产物、流程和运行时检查。
- `docs/downstream_upgrade_note.md`
  迁移说明，面向从旧版 registry 工作流切到当前 session 工作流的维护者。

## 快速使用

先构建：

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

如果当前项目的模块产物缺失，或与当前源码不同步，`prepare` 会尽早报错并提示先构建。确需由工具自动补构建时，可显式加：

```bash
python3 scripts/temporary_axiom_session.py prepare \
  --target YourProject.Section:goal \
  --auto-build
```

`prepare` 默认会在结束前做一次 prepare-time temporary-axiom hash verification，确认冻结下来的 permitted hashes 与当前 elaboration 一致。若只想先生成 session、把这轮校验留给后续 `lake build`，可显式加：

```bash
python3 scripts/temporary_axiom_session.py prepare \
  --target YourProject.Section:goal \
  --no-verify
```

结束后清理：

```bash
python3 scripts/temporary_axiom_session.py cleanup
```

外层工具应读取 `.temporary_axiom_session/session.json`，再取其中的 `freeze` 字段。

例如：

```python
session = json.load(open(".temporary_axiom_session/session.json"))
freeze = session["freeze"]
```

## Session 是什么

这里的一次 session，指的是围绕一个 target theorem，把当前 workspace 暂时准备成一次“受控证明尝试”的状态。

`prepare` 会做三类事：

- 冻结这次尝试要使用的信息：target theorem、module closure、允许临时公理化的声明及其 statement hash。
- 生成 Lean 侧 runtime：把冻结结果写入 `TemporaryAxiomTool/PreparedSession/Target.lean` 和 `TemporaryAxiomTool/PreparedSession/Permitted/**/*.lean`。
- 对源码做受控修改：预检和源码扫描会忽略上一次没清干净的 tool-managed 残留；真正落盘时，只修改这次确实需要打标记的源码文件。没有现成 attr block 的声明会插入独立的 managed `@[temporary_axiom]` 行；已有 attr block 的声明会把 `temporary_axiom` 合并进原 block。
- 默认再做一轮 prepare-time hash verification；`--no-verify` 会跳过这一步，改为信任 `prepare` 阶段离线 replay 得到的 frozen hash。

`prepare` 完成后，还会给用户输出一份摘要：

- 涉及模块数量
- permitted temporary axioms 数量
- 列表较短时，直接按模块打印哪些定理被转成临时公理
- 列表较长时，提示去看根目录下的明文报告

同一个 workspace 内不允许并发执行两个 `prepare`；如果另一个 `prepare` 正在运行，工具会直接报错并拒绝进入中间状态。

`--target` 支持两种输入：

- `<module>:<decl>`
- `<fully-qualified-decl>`

其中第一种里的 `<decl>` 可以是短名，也可以是 Lean 的完整声明名。

- `--target Foo.Bar:goal`
  会在定义模块 `Foo.Bar` 内按短名 `goal` 定向查找唯一匹配的声明。
- `--target Foo.Bar:My.Namespace.goal`
  会把 `My.Namespace.goal` 当成完整声明名处理，并要求它的定义模块正是 `Foo.Bar`。

第二种形式不会触发仓库扫描；工具只会按声明名前缀尝试有限个候选模块。若声明名与模块路径不对齐，应改用第一种形式。

为避免陈旧产物把标签插到错误位置，`prepare` 会在 preflight 里把旧 tool-managed 残留视作不存在，再做一轮轻量检查：

- 检查 `.ilean` / `.olean` / `.trace` 是否齐全。
- 检查当前源码 import 头是否仍与 `.ilean` 记录一致。
- 检查当前源码文本哈希是否仍与 Lake `.trace` 记录一致。

通过 preflight 后，`prepare` 还会继续做几类本地一致性检查：

- 用 Lean probe 确认目标声明仍能解析，并读取 target statement hash；对 permitted declarations 则离线 replay 成 axiom 语义后读取 axiom-side statement hash。
- 默认在写完 runtime 和源码 managed 修改后做一次 prepare-time hash verification；显式给 `--no-verify` 时跳过这一步。
- 写完 runtime、`session.json` 和源码 managed 修改后，会立刻做一次本地 manifest 自检。

这样做之后，proving agent 可以在 Lean 里继续工作，但只有这次 session 明确允许的 `sorry` 声明，才会被当作临时公理使用。

## 为什么会改源码

`@[temporary_axiom]` 的作用，不是做标记展示，而是把带标签的 `theorem` 改写成 `axiom`，并在 Lean elaboration 里立即检查它是否满足本次 session 的规则。

这也是 `prepare` 需要插入 import 和 attribute 的原因：

- `import TemporaryAxiomTool.PreparedSession.Target`
  让当前模块加载 frozen target runtime。
- `import TemporaryAxiomTool.PreparedSession.Permitted.<CurrentModule>`
  让当前模块只加载自己的 permitted runtime shard；这些 shard 自身再导入 `TemporaryAxiomTool.TemporaryAxiom`，从而拿到 attribute 和运行时检查逻辑。
- `@[temporary_axiom]`
  只加在本次允许跳过证明的声明上，使这些声明在当前 attempt 里临时按公理处理。

如果目标定理本身被打标签、未获许可的声明被打标签，或者声明头发生 hash drift，Lean 会在声明处直接报错。

## `session.json` 里有什么

`.temporary_axiom_session/session.json` 只有两个顶层部分：

- `freeze`
  给外层 verifier / comparator 读取的冻结信息。
- `cleanup`
  给本工具自己的 `cleanup` 命令使用的 edit log。

`freeze` 的语义是：

- `target`
  这次 attempt 的目标定理，以及它冻结时的 statement hash。`target.decl_name` 记录的是 Lean 的真实全限定声明名；`target.module` 记录的是定义模块名。
- `module_closure`
  这次收集 permitted declarations 时考察过的项目内模块闭包。
- `permitted_axioms`
  这次 attempt 中允许携带 `@[temporary_axiom]` 的声明列表。每条记录都带有 Lean 的真实全限定声明名、定义模块名、冻结时的 statement hash，以及来源类型。

外层工具通常只需要 `freeze`；`cleanup` 只用于撤销 TemporaryAxiomTool 自己插入的 managed 修改。

此外还会在仓库根目录生成：

- `temporary_axiom_tool_session_report.txt`
  给人读的明文报告，汇总 target、module closure 和按模块分组的 permitted temporary axioms。

## 当前分支相对 `main` 的主要区别

`module-sharded-session` 这条线和 `main` 的差别，主要不在 CLI 输入格式，而在 prepared runtime 的布局与 `prepare` 的默认策略：

- `main` 使用单个 `TemporaryAxiomTool/PreparedSession/Generated.lean`；当前分支改为 `Target.lean` 加 `Permitted/**/*.lean` 的分模块 runtime shard。
- `main` 的 `prepare` 默认会做一次 prepare-time hash verification，并提供 `--no-verify` 关闭；当前分支现在也保持同样的 CLI 语义，但 verify 时重写的是分 shard runtime。
- 两边都会先用离线 axiom replay 为 permitted declarations 生成初始 axiom-side hash；当前分支的区别不在 hash 来源，而在 generated runtime 按模块分 shard 写出。

## 文档

- [docs/temporary_axiom.md](docs/temporary_axiom.md)
- [docs/downstream_upgrade_note.md](docs/downstream_upgrade_note.md)
