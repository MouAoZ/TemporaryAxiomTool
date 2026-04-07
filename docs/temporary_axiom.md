# TemporaryAxiomTool 技术规格

这份文档只描述当前工具本身的技术行为。

相关文档：

- 快速使用见 [../README.md](../README.md)
- 迁移说明见 [downstream_upgrade_note.md](./downstream_upgrade_note.md)

## 1. 文件结构

```text
.
├── README.md                                   # 入口文档
├── temporary_axiom_tool_session_report.txt     # 生成物：给用户看的明文 session 报告
├── docs/
│   ├── temporary_axiom.md                      # 技术规格
│   └── downstream_upgrade_note.md              # 旧工作流迁移说明
├── scripts/
│   ├── temporary_axiom_session.py              # CLI 入口
│   └── session_tool/
│       ├── cli.py                              # prepare / cleanup 主流程
│       ├── common.py                           # 路径、JSON、模块路径辅助
│       └── lean_ops.py                         # 调 lake / lean probe，并写生成 runtime
├── TemporaryAxiomTool.lean                     # 库根模块
├── TemporaryAxiomTool/
│   ├── TemporaryAxiom.lean                     # `@[temporary_axiom]` 与 theorem -> axiom 改写
│   ├── PreparedSession.lean                    # probe 命令，读取 generated runtime
│   ├── PreparedSession/
│   │   ├── Types.lean                          # generated runtime 使用的数据结构
│   │   └── Generated.lean                      # 生成物：当前 session 的 target / permitted 表
│   ├── StatementHash.lean                      # elaborated statement hash
│   └── TestFixture/                            # 测试项目
│       ├── DepA.lean
│       ├── DepB.lean
│       └── Target.lean
└── .temporary_axiom_session/
    ├── prepare.lock                           # 生成物：`prepare` 运行期间的互斥锁
    └── session.json                           # 生成物：当前 session 的 freeze + cleanup 信息
```

说明：

- `TemporaryAxiomTool/PreparedSession/Generated.lean` 是生成物；没有活动 session 时会被重置为空 runtime。
- `.temporary_axiom_session/prepare.lock` 是瞬时生成物；只在 `prepare` 执行期间存在。
- `.temporary_axiom_session/session.json` 是生成物；只有 `prepare` 之后才存在。
- `temporary_axiom_tool_session_report.txt` 也是生成物；`cleanup` 会一并删除。

## 2. 命令

当前版本只提供两条命令：

```bash
python3 scripts/temporary_axiom_session.py prepare --target MyProj.Mod:goal
python3 scripts/temporary_axiom_session.py prepare --target MyProj.Mod:My.Namespace.goal
python3 scripts/temporary_axiom_session.py prepare --target MyProj.Mod.goal
python3 scripts/temporary_axiom_session.py cleanup
```

## 3. Session 语义

一次 session，是围绕一个 target theorem 建立的一次受控证明尝试环境。

`prepare` 会：

1. 冻结 target theorem 和本次允许临时公理化的声明。
2. 生成 Lean 侧 runtime，让 Lean 能读到这次冻结结果。
3. 对源码插入 managed import 和 managed `@[temporary_axiom]`。

这样做的目的，是让 Lean 在当前 workspace 里把“允许跳过证明的声明”临时当作公理使用，同时仍然在 elaboration 阶段检查：

- 该声明是否在 permitted 集合中；
- 它是否错误地等于 target；
- 它的 statement 是否相对冻结结果发生了漂移。

## 4. 产物

活动 session 会生成三个产物：

- `.temporary_axiom_session/session.json`
- `temporary_axiom_tool_session_report.txt`
- `TemporaryAxiomTool/PreparedSession/Generated.lean`

`session.json` 的稳定结构是：

```json
{
  "schema_version": 2,
  "base_commit": "optional git sha",
  "freeze": {
    "target": {
      "decl_name": "MyProj.Namespace.goal",
      "module": "MyProj.Mod",
      "statement_hash": "123"
    },
    "module_closure": ["MyProj.Mod", "MyProj.Dep"],
    "permitted_axioms": [
      {
        "decl_name": "MyProj.Dep.dep_sorry",
        "module": "MyProj.Dep",
        "statement_hash": "456",
        "origin": "dependency_module"
      }
    ]
  },
  "cleanup": {
    "edits": {
      "imports": [],
      "attributes": []
    }
  }
}
```

字段语义：

- `schema_version`
  当前 session 文件格式版本。
- `base_commit`
  调用方传入或自动读取的基准 commit 记录。
- `freeze`
  这次 attempt 的冻结信息，供外层 verifier / comparator 读取。
- `cleanup`
  本工具自己的 edit log，供 `cleanup` 回滚 managed 修改。

`freeze` 的子字段语义：

- `target`
  本次 attempt 的目标定理，以及它冻结时的 statement hash。`target.decl_name` 是 Lean 的真实全限定声明名；`target.module` 是定义模块名。
- `module_closure`
  为收集 permitted declarations 而考察过的项目内模块闭包。
- `permitted_axioms`
  允许携带 `@[temporary_axiom]` 的声明列表。每条记录里的 `decl_name` 也是 Lean 的真实全限定声明名，而不是由模块名和短名机械拼接出来的字符串。

`permitted_axioms[*].origin` 当前有两种来源：

- `prior_same_module`
  target 同模块且位于 target 之前。
- `dependency_module`
  target 依赖模块中的显式 `sorry` 声明。

`temporary_axiom_tool_session_report.txt` 的语义：

- 给用户看的明文摘要
- 汇总 target、base commit、module closure 和按模块分组的 permitted temporary axioms

## 5. `prepare`

`prepare` 的输入：

- `--target` 必填，支持两种格式：
  - `<module>:<decl>`
  - `<fully-qualified-decl>`
- `--base-commit <sha>` 可选；默认取当前 `HEAD`

流程：

1. 解析 `--target`：
   - 若是 `<module>:<decl>`，模块部分总是视为定义模块；声明部分若不含 `.`，则按“模块内短名”定向解析唯一匹配的声明，若含 `.`，则按完整声明名处理。
   - 若是 `<fully-qualified-decl>`，只按声明名前缀尝试有限个候选模块，不做仓库扫描。
2. 获取 `prepare.lock`，拒绝并发的第二个 `prepare`。
3. 读取 target 模块 `.ilean` 的 `directImports`，计算项目内 module closure。
4. 对 closure 中的每个模块读取 `.ilean` 的 `decls`。
5. 只在 declaration 自己的源码 range 内检查是否显式出现 `sorry`。
6. 对目标声明和候选 declaration 做轻量 range 校验：`.ilean` range 切出的源码片段必须仍然对应到声明名本身。
7. 对命中的 declaration 做 Lean probe，读取 statement hash。
8. 写出 `Generated.lean` 与 `session.json`。
9. 写出 `temporary_axiom_tool_session_report.txt`。
10. 在源码中插入 managed import 和 managed `@[temporary_axiom]`。
11. 立即用 `session.json`、generated runtime 和本次 edit log 做一次本地一致性自检。
12. 删除 `prepare.lock`。

当前 permitted 集合只包含两类声明：

- target 同模块且位于 target 之前的显式 `sorry` 声明
- target 依赖模块中的显式 `sorry` 声明

这里之所以要改源码，是因为运行时检查发生在 Lean elaboration 里：只有 import 了 `TemporaryAxiomTool.TemporaryAxiom`，并且真实给声明加上 `@[temporary_axiom]`，这些声明才会在当前 attempt 中按“受检查的临时公理”工作。

`prepare` 的命令行输出会额外给出：

- 涉及模块数量
- permitted temporary axioms 数量
- 列表较短时的按模块内联清单
- 详细数据所在路径

活动 session 的一致性检查只读取两类信息：

- `session.json` / `Generated.lean`
- 当前活动 session 的 `cleanup.edits` 里列出的源码文件

这些检查都是局部检查，不引入新的全仓库扫描。

## 6. 运行时检查

`@[temporary_axiom]` 会把带标签的 `theorem` 改写成 `axiom`，然后在类型检查后校验：

1. 该声明必须在本次 session 的 permitted 集合里
2. 该声明不能是 frozen target
3. 当前 elaborated statement hash 必须与冻结值一致

非法标签会直接在声明处报错。

## 7. `cleanup`

`cleanup` 不使用旧行号，而是只根据 `session.json.cleanup` 中记录的 managed edit log 清理：

1. 删除 managed import
2. 删除或还原 managed `@[temporary_axiom]`
3. 重置 `TemporaryAxiomTool/PreparedSession/Generated.lean`
4. 删除 `.temporary_axiom_session/session.json`
5. 删除 `temporary_axiom_tool_session_report.txt`
6. 尝试移除空的 `.temporary_axiom_session/`

## 8. 实现边界

- `.ilean` 提供 declaration range 与 import closure
- 源码扫描只负责显式 `sorry` 判定
- Lean probe 只负责 statement hash
- 不直接解析 `.olean` 二进制文件

## 9. 回归测试

```bash
lake build TemporaryAxiomTool.TestFixture.Target
python3 scripts/temporary_axiom_session.py prepare \
  --target TemporaryAxiomTool.TestFixture.Target:goal
python3 scripts/temporary_axiom_session.py prepare \
  --target TemporaryAxiomTool.TestFixture.Target.goal
lake build TemporaryAxiomTool.TestFixture.Target
python3 scripts/temporary_axiom_session.py cleanup
lake build TemporaryAxiomTool.TestFixture.Target
```
