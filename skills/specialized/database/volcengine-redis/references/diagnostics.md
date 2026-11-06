# 诊断取证

## 范围与时间线

取得 Region、精确实例 ID、产品类型、故障起止时间和时区、客户端现象及影响范围。
若时间未知，先问事件时间；可以先查当前配置，但不能用当前 Running 证明历史无异常。
统一 UTC，对齐客户端、监控、日志、任务与审计时间；比较故障前、故障中和恢复后的同粒度窗口。
先确认权限与产品支持。仅向用户询问工具无法取得的信息；不要求提交口令、完整连接串或业务 Key 值。

## 症状到证据

| 症状 | 首轮证据 | 交叉验证与判断边界 |
|---|---|---|
| 连接超时/拒绝 | 实例状态、地址端口、白名单、来源网络、连接量/连接使用率 | 经授权查询关联 VPC/子网/安全组和客户端网络；Running 不代表链路通。连接池耗尽与服务端连接上限分别验证，不默认开公网 |
| 认证失败/Readonly | 客户端脱敏错误、目标地址、账号权限/SSL配置、公开任务事件 | 区分认证、只读连接目标、角色变化与客户端行为；不以 API IAM 权限解释数据面密码问题，不主动重置密码 |
| 延迟/超时 | QPS、延迟、CPU、网络、连接量时间序列及慢日志 | 对比节点与聚合序列、读写流量及客户端时间；平均值正常不能排除尖峰。仅在指标提供时使用峰值/分位数，检查统计方法和单位 |
| 内存压力/淘汰 | 内存使用率、已用量、Key 数/淘汰速率、参数与规格 | 结合大 Key 的类型/字节数/元素数、分片倾斜和过期趋势；使用量变化不是规格变更，淘汰不自动等于泄漏 |
| CPU/带宽高/热点 | 节点 CPU、QPS、收发吞吐、公开限流指标（若支持）和热 Key | 对比热点样本时间、慢命令与分片差异；高 QPS 单独不能证明带宽限流，热点样本不是完整访问分布 |
| 操作失败/状态异常 | RequestId、DescribeTasks、操作审计、实例当前属性 | 按时间关联动作、身份、任务和结果；审计调用成功不代表异步任务成功，计划事件不代表已发生的历史操作 |

慢日志用来识别服务端耗时命令，不含全部客户端排队/网络耗时；空慢日志不能排除延迟。
大/热 Key 查询返回受采集策略、窗口与条数限制的样本，空样本不能证明没有热点。
按公开接口确认采集状态、留存与支持版本，不为了补样本自动创建扫描任务。
不要使用 KEYS *、全库遍历、抓取数据值或压测作为默认诊断手段。

## 云监控

依据：[Redis 指标](https://docs.volcengine.com/docs/cache/monitoring-metric?lang=zh)、
[GetMetricData](https://docs.volcengine.com/docs/cm/getmetricdata?lang=zh)。
社区版使用 VCM_Redis，公开子空间含 proxy、aggregated_proxy、server、aggregated_server；
企业版需另核实对应目录，不能复用社区版名称。
从目录确认 MetricName、单位、统计周期、支持的统计方法与 Dimensions。
例如 server 的 MemUtil 与 UsedMem 分别表达使用率和用量，不能直接比较原始数值。

每个请求显式包含 ResourceID；按节点查询时保留实例维度并补齐目录要求的节点维度。
不省略 Instances 查询整地域。聚合值与节点值不要再次混算；多个统计方法是不同序列。
保留时间戳、单位、粒度、统计方法和维度。缺点、无权限、超出留存均不填零。
使用同粒度与同口径对照窗口；指标更名/下线时核实当前目录，不不断猜名称请求。

## 操作日志与审计

任务、计划事件、CloudTrail 是三类证据；CloudTrail 不是 Redis 命令访问日志。
已安装 volcengine-cloud-trail 时按其事件检索流程查询绑定资源与固定窗口；否则核实
`ve cloudtrail LookupEvents --help --detail`，用 ResourceID 条件，StartTime/EndTime 为秒，
按 NextToken 翻页。不要猜 EventSource；仅资源条件可定位时不额外加入未核实事件源。
限定页数/时间预算，保留未完成状态。历史审计不包含旧值时不能完整重建历史配置。
关联事件身份、动作、资源、结果与 RequestId；共享角色不能直接归因某个自然人。

## 结论与下一步

输出已确认事实、候选原因及支持/反证、仍缺证据、建议动作和验证方式。
相关时间相近不等于因果；无法确定根因应明确写待证实。
建议参数调整、变配、扫描、切换或修复时给出影响、授权边界和回退可行性。
只请求诊断时不实施这些操作；明确授权后沿用变更流程。超出公开可见范围时，
整理实例/地域/时间窗口/脱敏错误/RequestId/指标现象供用户提交支持，不编造内部原因。

## 查询计划工具

在 skill 目录运行；替换目标和故障时间，不直接使用示例时间查询：

```bash
python3 scripts/diagnostic_plan.py --region '<region>' --instance-id '<instance-id>' \
  --start '<RFC3339-with-timezone>' --end '<RFC3339-with-timezone>' \
  --sources slow tasks audit
```

根据症状选择 sources，`big hot` 适用于样本取证，`metrics` 需要 `--metric-spec` JSON 文件。
metric spec 示例结构如下；使用前按实例类型与当前公开目录核实，并补齐所需节点维度：

```json
{"Namespace":"VCM_Redis","SubNamespace":"server","MetricName":"MemUtil","Period":"1m","StatisticsMethods":["avg"],"Dimensions":[]}
```

工具强制绑定 ResourceID 并拒绝覆盖；Dimensions 只补其他已核实维度。
计划窗口最多 24h 是本地查询预算，不代表所有 API 的官方留存期限。
脚本不会验证指标是否适用于该实例或自动补齐必需维度，执行前仍需目录校验。
输出包含资源标识，留存时放仓库外私有文件。执行 argv 时直接传参数数组，
不要拼 shell 字符串；设置入口要求的 caller metadata 并保留 CLI 安全策略。
每页先检查退出码和 API Error，再处理响应；最多十页是计划预算，触顶报 partial。
工具只构造首请求：慢日志 Context、审计 NextToken、任务分页均由执行者按契约续取，
不把生成计划或发出首请求写成“完成巡检”。
