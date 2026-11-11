---
name: volcengine-redis
description: >-
  Inspect and operate Volcengine managed Redis (缓存数据库 Redis 版): instance inventory,
  configuration and drift, connectivity, cloud monitoring, slow logs, big/hot keys,
  operation audit, incident diagnosis and health inspections, backup and change planning,
  and authorized lifecycle operations. Use for 火山 Redis instance IDs, Redis access or
  configuration questions, capacity/version compatibility, and application integration or
  recovery workflows. Not for unrelated self-hosted Redis or internal MetaRedis Kubernetes operations.
license: MIT
metadata:
  openclaw:
    requires:
      bins: [ve, python3]
      env: [VE_CALLER_TYPE, VE_CALLER_NAME, VE_SKILL_NAME]
    envVars:
      - name: VE_CALLER_TYPE
        required: true
        description: Agent sets ai_agent on every ve process.
      - name: VE_CALLER_NAME
        required: true
        description: Actual agent host identifier, resolved from runtime, not a model or account.
      - name: VE_SKILL_NAME
        required: true
        description: volcengine-redis for this workflow, including helper child processes.
      - name: VOLCENGINE_ACCESS_KEY
        required: false
        description: Existing CLI authentication; never print or embed credentials.
      - name: VOLCENGINE_SECRET_KEY
        required: false
        description: Existing CLI authentication; prefer configured login/profile or STS.
      - name: VOLCENGINE_SESSION_TOKEN
        required: false
        description: Required with temporary credentials supplied by the environment.
      - name: VOLCENGINE_REGION
        required: false
        description: CLI fallback only; resolve the task region explicitly.
      - name: VOLCENGINE_PROFILE
        required: false
        description: Existing CLI identity; do not switch identities without authorization.
---

# 火山引擎 Redis

将用户意图转换为有依据的实例查询、变更方案和已授权操作。默认使用 `ve redis`，
业务判断留在本 skill；CLI 安装、认证、参数发现和安全策略沿用已安装的
`volcengine-cli`。没有该 skill 时使用当前 `ve` 帮助与官方 API 文档，不依赖内部系统。

## 从实际请求决定行动

- “查一下、解释、评估、给方案、先确认”只授权对应查询或方案，不执行备份、变配、
  开公网、改白名单或切换。明确要求执行且对象、目标参数和影响范围已确定时，进入变更流程。
  无参或缺参数的写 API 探测也属于实际写调用，即使返回 400；参数发现只读帮助或公开契约。
  尊重已有明确授权，不重复索取同一授权；工具自身的确认门禁按 CLI 规则处理。
- 从输入或明确的资源绑定取得 Region 和实例 ID。绑定与题面冲突先澄清；不从 ID 前缀、
  名称、历史样例或 CLI 默认地域猜 Region。缺 Region 时询问，或用已有授权的资源定位工具。
  有 ID 就精确查询，不先扫描全账号。只有名称时注意同名和模糊匹配，列候选而非取第一台。
- 明确是社区版还是企业版、集群还是非集群；同一个 Redis 协议不代表相同的产品 API、
  参数、命令、模块或恢复能力。返回的 `ServiceType` 决定是否查询 Enterprise 对应接口。
- 能从查询得到的版本、地址、拓扑不再让用户手填。客户端错误、来源网络、时间窗口、
  目标规格等不能推导的信息集中追问；先交付已能完成的部分。

## 取证与交付

1. 使用已核实的只读契约执行最小相关查询。契约未知时定向查
   `ve redis <Action> --help --detail`，按[API 与证据](references/api-evidence.md)选 Action。
   每个 `ve` 进程都设置 `VE_CALLER_TYPE=ai_agent`、当前宿主的 `VE_CALLER_NAME`、
   `VE_SKILL_NAME=volcengine-redis`，包括帮助、脚本、重试和轮询。保留安全策略原值。
2. 先检查进程退出状态和 `ResponseMetadata.Error`，再解析 `Result`。
   保存必要的查询范围、时间、RequestId 和脱敏证据；解析失败重读已存响应，不为排版重查云端。
   分页完整才能说“全部”。无权限、查询失败、未查询、查无记录、功能未启用不能互相替代。
3. 按[场景判断](references/workflows.md)解释产品语义。特别区分每分片节点数与总节点数、
   已用内存与规格容量、实例 Running 与客户端可连接、任务已提交与任务成功。
4. 回答先给本题结论，再给实际查到的关键事实/证据与缺口、必要的下一步。
   不强制所有任务输出同一大模板；用户要求结构化结果时提供 target、facts、evidence、
   missing_information、execution_status。未知值不填成 0、正常或成功。
   涉及计费、配额、模块开白和版本支持时提供适用的官方来源，区分通用规则与当前账号事实。

## 有界盘点与漂移对比

`scripts/redis_snapshot.py` 只调用 `DescribeDBInstances`，为盘点、目标定位及创建基线对比
生成精简 JSON。它不是全部配置审计：白名单、参数、SSL、备份和数据变化需另取对应证据。

在 skill 目录下先查看参数：

```bash
python3 scripts/redis_snapshot.py --help
```

执行时显式传 `--region`，用重复 `--instance-id` 限定目标；按项目盘点才使用 `--project`。
只读 API 结果按页收集并去重；总数漂移、重复页或超出 `--max-pages` 会报不完整，不交付伪全量。
必须通过 `--output` 保存私有快照，标准输出只给数量摘要；通过 `--baseline` 对比相同 scope 的本地快照。
运行产物留在仓库外，不提交资源 ID、网络地址或账号信息。脚本继承当前认证与安全策略，
不读取凭证文件、不补 `--yes`、不切换 endpoint/profile、不自动重试。

漂移输出区分配置、瞬时状态与使用量；没有差异只能说明所覆盖字段在两次观测时相同，
不能证明期间没有重启/改后还原，也不能证明 Key 数据没变。字段缺失是证据缺口，非属性删除。

## 诊断与巡检

延迟、连接、内存、热点或变更异常读取[诊断取证](references/diagnostics.md)；
健康检查、容量趋势和成本治理读取[巡检](references/inspection.md)。只调用公开产品接口。
先绑定地域、实例和带时区的时间窗口，再按症状选择证据，不把所有日志无差别拉取。

`scripts/diagnostic_plan.py` 生成只读查询计划（不会调用云端），可统一不同接口的时间单位：
`python3 scripts/diagnostic_plan.py --help`。执行计划前核实实例产品类型、接口支持和当前帮助，
按输出中的 continuation 规则读取后续页；计划生成成功不代表取证完成或实例健康。
监控请求需要传入从当前公开指标目录核实的 metric spec，不能凭名称猜测指标。
核实 CLI 的 JSON body 支持及响应契约后，可用 `scripts/diagnostic_collect.py` 执行计划；
见[只读取证执行](references/collection.md)。它只支持列明的诊断只读接口，不替代已授权变更流程。
参数/指标错误后先取得新契约依据再修正，不循环试名称或页大小；相同范围优先复用已存结果。

对外结论仅基于公开文档、用户提供的证据和授权的云接口。不要引用内部源码、研发平台、
私有日志/监控入口或未公开实现细节；公开证据不足时交付缺口与脱敏支持工单材料。

## 实际变更

执行前读取[变更与编排](references/changes.md)。本 skill 支持按已授权目标组织操作，
但不把“为了排障/为了让评测有数据”视为写授权。

- 核实对象、当前值、目标值、影响范围和前置条件；先形成可审查计划与回退边界。
- 使用真实 API 契约，不给不支持的 Action 添加通用 `DryRun`。Terraform 场景沿用用户指定
  provider；选择 `volcenginecc` 时核对当前 schema、Region 和现有 state，避免重新创建已存在资源。
- CLI 拒绝/要求确认时遵守该门禁，不经 SDK、HTTP 或 Terraform 绕过。
  不关闭安全策略、不默认全网放行或关闭 SSL/认证。
- 异步写操作记录任务/资源 ID；回查目标终态和请求的具体属性。失败/超时先查是否已经执行，
  不盲重试创建、升级、恢复或切换。
- 写操作记录脱敏本地 ledger：地域、资源、动作、前后值、任务/RequestId、验证结果及未完成项。
  “恢复基线”是显式清理/回退步骤，不是默认已实现的自动能力；不可逆操作不能许诺原地撤销。

## 参考与维护

- [API 与证据](references/api-evidence.md)：选取查询入口、分页及错误语义。
- [场景判断](references/workflows.md)：知识、成本、网络、监控、盘点与诊断。
- [变更与编排](references/changes.md)：白名单、变配、备份、恢复和跨产品依赖。
- [官方文档主页](https://docs.volcengine.com/docs/cache?lang=zh)。

离线测试位于仓库 `tests/test_redis_snapshot.py`；行为验收位于
`tests/redis_skill_cases.json`。评测答案、SC/L 标签与固定实例拓扑不是运行时产品事实；
不能用参考答案替代查询，不能只因 L2/L4 标签就执行写操作或编造跨产品关联。
