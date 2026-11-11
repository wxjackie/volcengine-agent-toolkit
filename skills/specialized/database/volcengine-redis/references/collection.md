# 只读取证执行

`diagnostic_collect.py` 执行 `diagnostic_plan.py` 生成的 slow/big/hot/tasks/audit/metrics
计划。它不执行计划中的 argv，而是验证并重建绑定目标的请求；仅允许六个指定只读 Action。
CreateBackup、CreateKeyScanJob 等不在此工具内。真正已授权的变更仍按 changes.md 执行。

## 执行前

先沿 volcengine-cli 确认当前入口可用、Action 帮助及 `--body`/`--output json` 支持。
未兼容时修复工具环境或明确缺口，不反复请求、不绕过安全门禁。本执行器不会安装或升级 CLI。
metric spec 必须从当前公开目录核实名称、子空间、必需维度及统计方法；脚本只校验结构，
不会替代目录验证。不要为补数据执行扫描、压测或写请求。

生成计划，按实际响应契约创建 contracts JSON（不要把示例字段当成所有接口的字段）：

```json
{
  "DescribeSlowLogs": {
    "source": "当前公开文档URL或CLI帮助版本与核验日期",
    "items": "Result.<已核实的记录数组字段>",
    "done": "Result.ListOver",
    "cursor": "Result.Context"
  }
}
```

每个选中 Action 都要给 source 和 items；路径用点分隔的对象字段，仅允许 Result 内的数组。
SlowLogs 另需 done/cursor；Tasks 需 total（当前响应的总数路径）；LookupEvents 需 cursor
（NextToken 路径）。未知字段不猜，停止该分支。若契约允许省略末页 NextToken，当前工具会
保守报 partial，需人工按该契约核验，不能伪造字段后声称自动验证完成。
Big/Hot 为有界样本，监控为序列集合；二者不使用总数或游标分页。

```bash
python3 scripts/diagnostic_plan.py --region '<region>' --instance-id 'redis-<id>' \
  --start '<RFC3339-with-timezone>' --end '<RFC3339-with-timezone>' \
  --sources slow tasks > /private/path/plan.json
python3 scripts/diagnostic_collect.py --plan /private/path/plan.json \
  --contracts /private/path/contracts.json --output /private/path/evidence.json
```

父进程设置 SKILL.md 要求的三项 caller metadata，继承当前身份及安全策略。脚本不读取凭证文件，
不输出环境或原始错误，不添加确认绕过参数。输出使用新建的 0600 文件，拒绝覆盖和符号链接。
同一查询优先回读该文件；重查必须有时效、轮询或写后验证目的，另存新文件。

## 结果含义

- complete：列表按核实的终止字段闭合，仅说明这次查询范围完整，不证明实例健康。
- sample：大/热 Key 有界样本，空样本不能证明不存在大/热 Key。
- unverified_coverage：监控序列已返回，仍需核对实例/节点维度、时间点、单位及缺点；空数组不是零流量。
- error：没有可用页；CLI/API 错误、空对象、缺字段不能转换为空数据。
- partial：已有部分证据，但分页、响应契约、重复页、总数漂移或预算阻止完整取证。

退出码 0 只说明没有 error/partial，不表示监控覆盖完整或检查通过；2 表示存在 error/partial；
1 是计划/本地配置错误。按 source 独立查看状态，不给缺项巡检整体通过结论。
默认每个 source 最多 10 页，每次请求最多 60 秒，失败不自动重试；更窄任务可降低页数预算。

pages 保存受限范围的原始记录供本地核验，可能含 Key 名、命令参数或审计业务信息；
它不是可直接对外分享的脱敏报告。只在私有目录留存，回答使用脱敏摘要，不贴凭证、数据值或下载URL。
采集时间由系统 UTC 单独记录，RequestId 只作为标识。
