# AI 使用记录

> 谁用谁记，当天记，不要事后补。报告第五部分与答辩都要用这里的细节。

## 记录表

| 日期 | 使用人 | 工具 | 环节 | 让它做了什么 | 人工改了什么 | 采纳程度 |
|------|--------|------|------|--------------|--------------|----------|
| 2026-10-27 | 兰天 | TraeCode（DeepSeek-V4.1-Flash） | 编码 | 排查 .env 未生效：新增 config/env.py 最小解析，并在 settings、consumer、simulator 三个入口最早处加载 | 人工复核并验证：setdefault 优先级、BASE_DIR 定位、三处加载时机；pytest 30 passed、manage.py check 无 issue、.env 正/错密码 migrate 反证与环境变量压过 .env 均通过 | 直接采用 |
| 2026-11-10 | 兰天 | TraeCode | 测试 | 补齐 stream 模块单元测试缺口：指标口径 §3-4 的 line_id 缺失归空串、total 与 stations 之和一致性约束、契约 v1.1 结果结构校验；consumer.py persist_window 的 ignore_conflicts 幂等、MySQL 降级只告警一次、迟到-only 窗口也要落库 | AI 第一次写 consumer 侧测试时 patch 目标错了（写了 stream.consumer.MetricWindow 但 persist_window 里是延迟 import，实际在 api.models.MetricWindow），报 AttributeError；人工改正后重新生成。MagicMock 构造的对象属性不能直接断言，AI 最初写的 rows[0].station_id == "A01" 断言失败，人工改成 fake_model.call_args kwargs 检查后通过。最终 pytest 38 passed | 直接采用（修正了两处 mock 使用） |
| 2026-11-10 | 兰天 | TraeCode | 文档 | 写 GitHub PR 描述，按仓库 PR 模板填改动内容、涉及目录、验证方式、自查清单、关联任务卡 | 人工核对了 diff --stat 输出确保"涉及目录"勾选准确，自查清单勾选项全部人工再跑一遍确认属实 | 直接采用 |

## 填写说明

- **工具**：写具体名字（Cursor / Copilot / 通义灵码 / 文心快码 / ChatGPT），不要只写"AI"。
- **环节**：需求分析 / 系统设计 / 编码 / 测试 / 文档。
- **让它做了什么**：一句话说清任务，不写"帮我写代码"。
- **人工改了什么**：评分重点，写具体动作——补了哪个分支、换了哪个字段、删了哪段、为什么删。
- **采纳程度**：直接采用 / 部分采纳 / 仅参考 / 否决。

## 典型 Prompt 留档

挑 1—2 个效果最好或最失败的，把完整 Prompt 和 AI 输出摘要贴在这里。下面只是格式参考，替换成实际用过的内容。

```
背景：项目是实时客流监测，事件从 Redis Stream 的 gate_events 读取，
      字段见 docs/02-设计/接口契约.md 第 2.1 节，口径见指标口径文档。
任务：实现 1 秒窗口聚合，按 station_id 累加 passengers，并在返回里带上 line_id。
约束：只改 stream/metrics.py；纯函数，不许在函数里读系统时间；
      迟到超过 2 个窗口的事件只计数不计入客流。
输出：先给改动说明和完整函数，不要直接改文件。
```

被否决的那条也要留，写明否决理由（字段名对不上契约、引入了课程不允许的依赖、把窗口值算成了累计值等），报告"教训"那段直接用。

被否决的方案（2026-10-27，编码环节）：

- 用 python-dotenv 加载：requirements.txt 是共享文件，加依赖要全组同意，否决。
- 用 os.environ.update() 或直接赋值：会让 .env 压过 compose 注入的
  MYSQL_HOST=mysql、REDIS_HOST=redis，容器模式连不上库，否决。
- 最终采用 config/env.py + os.environ.setdefault。
