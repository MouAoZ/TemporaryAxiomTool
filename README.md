# TemporaryAxiomTool

`TemporaryAxiomTool` 用来为一次 Lean 4 证明会话准备环境。

它维护两类数据：

- 当前活动 session 的冻结信息
- 本地持久的 proved theorem 数据库

活动 session 中，工具会按 theorem-side statement hash 校验声明头，并把允许跳过的定理直接当作同名公理注册；目标定理本身仍按普通 `theorem` elaboration。

## 文档

- [docs/temporary_axiom.md](docs/temporary_axiom.md)
  主用户手册。包含接入方式、命令、输出文件和常见错误。
- [docs/downstream_upgrade_note.md](docs/downstream_upgrade_note.md)
  从旧版 `PreparedSession` / managed-attr 工作流迁移到当前架构时的更新说明。

## 快速开始

先构建工具：

```bash
lake build TemporaryAxiomTool
```

把需要跟踪的项目模块接入工具：

```lean
import TemporaryAxiomTool
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

若需要让工具自动补构建解析目标或检查产物时依赖的模块，可加：

```bash
python3 scripts/temporary_axiom_session.py prepare \
  --target YourProject.Section:goal \
  --auto-build
```

结束当前 session：

```bash
python3 scripts/temporary_axiom_session.py cleanup
```

## 你会看到的文件

- `.temporary_axiom_session/session.json`
  当前活动 session 的 machine-readable freeze 数据
- `temporary_axiom_tool_session_report.txt`
  当前活动 session 的人类可读摘要
- `.temporary_axiom_registry/proved_theorems.json`
  本地持久 proved theorem 数据库
- `TemporaryAxiomTool/TheoremRegistry/Shards/**/*.lean`
  工具生成的 per-module shard

## 外层工具应读取什么

外层调度器或 comparator 应读取 `.temporary_axiom_session/session.json`。

其中最常用的是：

- `freeze.target`
  当前目标定理的 `decl_name`、定义模块 `module`、`statement_hash`
- `freeze.permitted_axioms_list`
  当前 session 的完整 permitted theorem 名字数组
- `freeze.permitted_axioms`
  带 `module`、`statement_hash`、`origin` 的完整 permitted 条目

如果需要“完整 permitted 集合加 target 名字”，读取：

- `freeze.permitted_axioms_list`
- `freeze.target.decl_name`

## 接入后的源码变化

- 每个 tracked module 都会获得一条稳定 shard import：

  ```lean
  import TemporaryAxiomTool.TheoremRegistry.Shards.<Module>
  ```

  这条 import 会保留在源码里。

- 若 target closure 里某个未 tracked 的普通模块命中了本次 session 的显式 `sorry` theorem / lemma，`prepare` 会临时给它插入对应 shard import；`cleanup` 会删除这类临时 import。

详细说明见 [docs/temporary_axiom.md](docs/temporary_axiom.md)。
