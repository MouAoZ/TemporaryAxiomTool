# 已批准陈述注册库数据库格式规格

## 作用范围

这个目录保存 `TemporaryAxiomTool` 的外部注册库。

它不是宿主数学库的一部分，而是由脚本维护的离线数据库层，用来支持：

- 可跳过 theorem 的批准登记
- statement hash 审计
- 人工审核 `status`
- 人工审核 `commit`
- statement 版本历史记录

统一管理入口：

- [../scripts/manage_approved_statement_registry.py](../scripts/manage_approved_statement_registry.py)

Lean 编译时不会直接读取这里的 JSON；脚本会先把 `current/` 转换成 Lean 侧生成模块，再由 Lean 运行时消费这些生成结果。

## 全局约束

对整个数据库有几个重要约束：

1. `current/` 是活动批准陈述的唯一真相来源
2. 同一个 `decl_name` 在整个 `current/` 中至多出现一次
3. `status` 与 `commit` 属于人工审核元数据，不产生 history 事件
4. `history/` 只记录 statement hash 变化
5. JSON 规范化由脚本负责，正常使用时不应手工编辑
6. 当前版本不维护旧格式兼容；如果手工修改 JSON，请直接遵守本文格式

当前仓库只保留空目录模板，不附带任何宿主 theorem 数据。

## 目录结构

- `current/`: 当前批准快照，按 chapter/section 分片
- `history/`: statement hash 变化历史

补充说明：

- `current/` 中空 shard 会被脚本自动删除
- `history/` 不参与 Lean 编译期判定

## Lean 侧生成物的对应关系

`current/` 会被脚本转换为：

- [../TemporaryAxiomTool/ApprovedStatementRegistry/Generated.lean](../TemporaryAxiomTool/ApprovedStatementRegistry/Generated.lean)
- `../TemporaryAxiomTool/ApprovedStatementRegistry/Shards/` 下的内部分片模块

Lean 运行时真正消费的只是每条批准记录中的：

- `decl_name`
- `statement_hash`
- `shard_id`

其他字段仅服务于离线管理和人工审计。

## `current/` 当前快照

### 文件命名

格式：

- `approved_statement_registry.chapter_<CC>.section_<SS>.json`

示例：

- `approved_statement_registry.chapter_03.section_02.json`

### 顶层结构

每个 shard 文件结构如下：

```json
{
  "shard_id": "chapter_03.section_02",
  "chapter": 3,
  "section": 2,
  "entries": []
}
```

顶层字段说明：

- `shard_id`: 分片编号，格式为 `chapter_<CC>.section_<SS>`
- `chapter`: chapter 编号
- `section`: section 编号
- `entries`: 当前分片下的已批准声明列表

约束：

- `shard_id` 必须与 `chapter`、`section` 一致
- `entries` 为空时，该 shard 文件通常会被脚本删除
- 通过脚本写入时，`entries` 会按 `decl_name` 排序

### `entries` 条目字段

每个条目表示一个当前仍然有效的已批准声明。

#### Lean 运行时直接依赖字段

- `decl_name`
  - 含义: Lean 全限定声明名
  - 用途: 运行时查找批准记录的主键
- `statement_hash`
  - 含义: theorem elaborated type 的稳定哈希值
  - 存储格式: 十进制字符串
  - 用途: `@[temporary_axiom]` 校验时与当前声明的真实 hash 比较

补充说明：

- 这两项会被转译进 Lean 侧生成模块
- `shard_id` 虽然不存放在 entry 内，而是来自 shard 顶层，但运行时错误消息也会依赖它

#### 离线探测与人工审阅字段

- `module`
  - 含义: 用于重新 probe 该声明的 Lean 模块
  - 用途: 离线审计时按模块分组探测
- `statement_pretty`
  - 含义: 供人工审阅的可读陈述
  - 用途: `report`、人工核查与故障排查

#### 审核元数据

- `status`
  - 含义: 人工审核状态
  - 取值: `safe`、`needs_attention`、`unreliable`
  - 语义: 显式字段，不从 `commit` 推导
- `commit`
  - 含义: 人工评论列表
  - 语义: 默认覆盖，显式 `--append` 才追加
  - 结构: 数组，每项都必须是对象，包含 `timestamp`、`author`、`message`

约束：

- `status` 与 `commit` 互不依赖
- 可以只改 `status` 不改 `commit`
- 也可以只改 `commit` 保持 `status = safe`
- 改 `status` 或 `commit` 不会写入 `history/`

#### 生命周期元数据

- `approved_by`: 最近一次执行 `approve` 的操作者
- `approval_reason`: 最近一次 `approve` 的说明
- `created_at`: 首次进入 current 的时间
- `updated_at`: 最近一次修改 current 条目的时间
- `approved_at`: 最近一次执行 `approve` 的时间

时间戳格式：

- UTC ISO 字符串，例如 `2026-03-31T01:02:03Z`

### 条目级语义约束

- `decl_name` 在整个 `current/` 中必须唯一
- re-approve 同一个 `decl_name` 时，旧条目会被覆盖，而不是并存
- 若 re-approve 到新的 chapter/section，条目会从旧 shard 移动到新 shard
- 若 re-approve 时 `statement_hash` 未变化，则保留原有 `status` 与 `commit`
- 若 re-approve 时 `statement_hash` 变化，则清空原有 `commit`，并把 `status` 重置为 `needs_attention`
- 只有 `statement_hash` 变化时才写入 `history/`

### 完整示例

```json
{
  "shard_id": "chapter_03.section_02",
  "chapter": 3,
  "section": 2,
  "entries": [
    {
      "decl_name": "YourProject.someTheorem",
      "module": "YourProject.Section2",
      "statement_pretty": "forall {P Q : Prop}, P ∧ Q -> Q ∧ P",
      "statement_hash": "1604932984",
      "status": "needs_attention",
      "commit": [
        {
          "timestamp": "2026-03-31T01:02:03Z",
          "author": "reviewer",
          "message": "binder order changed; manual review suggested"
        }
      ],
      "approved_by": "ai-agent",
      "approval_reason": "approved statement freeze",
      "created_at": "2026-03-30T22:10:00Z",
      "updated_at": "2026-03-31T01:02:03Z",
      "approved_at": "2026-03-30T22:10:00Z"
    }
  ]
}
```

## `history/` statement 版本历史

`history/` 下每个 JSON 文件表示一次 statement hash 变化。

当前脚本只在下面这种情况下写入 history：

- 某个已经存在于 current 中的声明被重新 `approve`
- 且新旧 `statement_hash` 不同

不会写入 history 的操作：

- 初次 `approve`
- `commit`
- `prune`
- `status` 更新
- `commit` 覆盖、追加、清空、删除单条

### 顶层字段

- `decl_name`
- `timestamp`
- `before_shard`
- `after_shard`
- `before`
- `after`

字段说明：

- `decl_name`: 发生 statement 版本变化的声明名
- `timestamp`: 变化被记录的时间
- `before_shard`: 旧版本所在 shard
- `after_shard`: 新版本所在 shard
- `before`: 旧版本条目快照
- `after`: 新版本条目快照

注意：

- `before` 与 `after` 使用的都是与 `current` entry 相同的规范化格式
- 即使 `status` 和 `commit` 在新版本中被重置，它们也会随 `before/after` 一起保存在记录里，方便人工追溯

### 完整示例

```json
{
  "decl_name": "YourProject.someTheorem",
  "timestamp": "2026-03-31T03:04:05Z",
  "before_shard": {
    "shard_id": "chapter_03.section_02",
    "chapter": 3,
    "section": 2
  },
  "after_shard": {
    "shard_id": "chapter_03.section_02",
    "chapter": 3,
    "section": 2
  },
  "before": {
    "decl_name": "YourProject.someTheorem",
    "module": "YourProject.Section2",
    "statement_pretty": "forall {P Q : Prop}, P ∧ Q -> Q ∧ P",
    "statement_hash": "1604932984",
    "status": "safe",
    "commit": [
      {
        "timestamp": "2026-03-31T01:02:03Z",
        "author": "reviewer",
        "message": "checked previous frozen statement"
      }
    ],
    "approved_by": "ai-agent",
    "approval_reason": "approved statement freeze",
    "created_at": "2026-03-30T22:10:00Z",
    "updated_at": "2026-03-31T01:02:03Z",
    "approved_at": "2026-03-30T22:10:00Z"
  },
  "after": {
    "decl_name": "YourProject.someTheorem",
    "module": "YourProject.Section2",
    "statement_pretty": "forall {P Q : Prop}, P -> Q -> Q ∧ P",
    "statement_hash": "2718281828",
    "status": "needs_attention",
    "commit": [],
    "approved_by": "ai-agent",
    "approval_reason": "approved statement freeze",
    "created_at": "2026-03-30T22:10:00Z",
    "updated_at": "2026-03-31T03:04:05Z",
    "approved_at": "2026-03-31T03:04:05Z"
  }
}
```

## 使用建议

- 不手工编辑 JSON，优先通过脚本维护
- 如果 `current/` 里出现同一 `decl_name` 的重复条目，先修复重复，再执行 `generate`
- 若怀疑 current 与 Lean 侧生成物不同步，优先执行 `generate`，再执行 `audit`
- 审核交接时优先使用 `report`，而不是直接手翻 JSON
- 若只关心陈述版本变化，查看 `history/`；不要期待它记录 `commit/status` 的每次改动
