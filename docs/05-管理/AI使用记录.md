# AI 使用记录

> 谁用谁记，当天记，不要事后补。报告第五部分与答辩都要用这里的细节。

## 记录表

| 日期         | 使用人      | 工具                                  | 环节 | 让它做了什么                                                                                                                                                                                                                                                                                                                          | 人工改了什么                                                                                                                                                                                                                                                                                                                                                                           | 采纳程度                  |
| ---------- | -------- | ----------------------------------- | -- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------- |
| 2026-10-27 | 兰天       | TraeCode（DeepSeek-V4.1-Flash）       | 编码 | 排查 .env 未生效：新增 config/env.py 最小解析，并在 settings、consumer、simulator 三个入口最早处加载                                                                                                                                                                                                                                                      | 人工复核并验证：setdefault 优先级、BASE_DIR 定位、三处加载时机；pytest 30 passed、manage.py check 无 issue、.env 正/错密码 migrate 反证与环境变量压过 .env 均通过                                                                                                                                                                                                                                                         | 直接采用                  |
| 2026-11-10 | 林世韬      | TraeCode（DeepSeek-V4.1-Flash）       | 编码 | 采集模块整改：站点配置外置 stations.json、corrupt 注释契约号 2.3→2.2、replay 时间戳平移到当前时间、补 tests/test_simulator.py 四场景                                                                                                                                                                                                                               | 人工核对：契约 2.1 六字段、2.2 异常类型、权重与线路映射；回放首条对齐当前时间且间隔不变；pytest 39 passed、manage.py check 无 issue                                                                                                                                                                                                                                                                                        | 直接采用                  |
| 2026-11-10 | 兰天       | TraeCode                            | 测试 | 补齐 stream 模块单元测试缺口：指标口径 §3-4 的 line_id 缺失归空串、total 与 stations 之和一致性约束、契约 v1.1 结果结构校验；consumer.py persist_window 的 ignore_conflicts 幂等、MySQL 降级只告警一次、迟到-only 窗口也要落库                                                                                                                                                              | AI 第一次写 consumer 侧测试时 patch 目标错了（写了 stream.consumer.MetricWindow 但 persist_window 里是延迟 import，实际在 api.models.MetricWindow），报 AttributeError；人工改正后重新生成。MagicMock 构造的对象属性不能直接断言，AI 最初写的 rows[0].station_id == "A01" 断言失败，人工改成 fake_model.call_args kwargs 检查后通过。最终 pytest 38 passed                                                                                                | 直接采用（修正了两处 mock 使用）   |
| 2026-11-10 | 兰天       | TraeCode                            | 文档 | 写 GitHub PR 描述，按仓库 PR 模板填改动内容、涉及目录、验证方式、自查清单、关联任务卡                                                                                                                                                                                                                                                                              | 人工核对了 diff --stat 输出确保"涉及目录"勾选准确，自查清单勾选项全部人工再跑一遍确认属实                                                                                                                                                                                                                                                                                                                             | 直接采用                  |
| 2026-11-13 | xuzhaopu | CodeBuddy Code（DeepSeek-V4.1-Flash） | 编码 | 大屏信息增强：新增 SVG 线网客流图、柱子较上一秒增减标签（散点承载涨跌双色）、排行点击下钻并联动柱状图高亮与单站趋势、迟到/非法合并为数据健康卡并附 60 秒火花线、告警条补持续时长与峰值、页面宽度随视口自适应；全程只用契约 v1.2 已有字段，未改 api/                                                                                                                                                                                            | 人工复核后定稿：先让 AI 出浅色/霓虹/极简三套风格备选，看图后否决，要求回到原深色方案；核对线网图与柱状图取同一份快照、下钻复用 /api/series 的 station_id 参数、空数据与断线不白屏；pytest 58 passed、manage.py check 无 issue、1920×1080 渲染确认无溢出                                                                                                                                                                                                              | 部分采纳（三套风格备选否决，其余直接采用） |
| 2026-11-16 | xuzhaopu | CodeBuddy Code（DeepSeek-V4.1-Flash） | 编码 | 大屏增加默认收起的「帮助」抽屉：指标口径与适用边界、异常现象对照表加四步处置、FAQ 与三个接口入口；首屏只多一个按钮，不动数据逻辑                                                                                                                                                                                                                                                              | 人工判断首版 5 个标签过重，要求精简为 3 个：删掉角色速查、三场景段、链路节点卡、页面地图、命令表与 2 条重复 FAQ，口径表由 4 列 7 行压为 2 列 5 行；净增行数从 272 降到 161；复核 pytest 58 passed、check 无 issue、三个标签页渲染正常                                                                                                                                                                                                                               | 部分采纳（结构与内容经一轮删减）      |
| 2026-11-17 | xuzhaopu | CodeBuddy Code（DeepSeek-V4.1-Flash） | 编码 | 大屏视觉改版：按 ui 参考稿把深色主题换成浅灰底 + 白卡片，重写样式表；KPI 改衬线大数字与圆点标签，柱状/趋势/线网图同步换色，切换控件改胶囊分段，帮助抽屉改白底下划线标签页                                                                                                                                                                                                                                     | 人工提供 `task/ui` 下五张参考图并要求按此风格改，否决了继续沿用深色方案；复核时发现 2 列布局下线网图被 SVG 固有高度撑满整屏，要求修掉（改为绝对定位 + 固定高度）；pytest 58 passed、check 无 issue、1920 与 1440 两种宽度渲染确认；随后起真实链路（MySQL/Redis/消费者/模拟器）看过实际效果                                                                                                                                                                                               | 直接采用（含一处布局缺陷修复）       |
| 2026-11-17 | 梁林祎      | TraeCode（DeepSeek-V4.1-Flash）       | 编码 | 接口契约 v1.3 ：`metric_window` 增 `in_passengers`/`out_passengers` 两列与迁移；`/api/series` 扩 `line_id`/`direction`/`granularity`/`from`/`to` 五个参数、点结构增 `in`/`out`/`total` 并保住 `passengers`；`/api/rank` 扩 `minutes`/`line_id`/`direction`；新增 `/api/summary/today`（今日累计、进/出、净流入、峰值、分线分站 TopN、数据质量）与 `/api/stations`（只读采集端 `stations.json`） | 人工核对契约字段名与实际 JSON 一致；否决用站名顶替线路名，改为只认 `line_name` 否则 `null`；发现 `/api/rank?direction=in` 在 `in` 列全 0 时按 0 排序导致名次与数字矛盾，改为取全量后按算出值排序；发现 `updated_at` 原先输出 UTC 与 `since` 混用两种时区，改为统一本地；补时区 `+` 被查询串解析成空格的解决方法；删掉无调用者的 `split_direction` 与两个纯转发包装；实测发现 `?direction=out` 把合并乘降量报成出站量，改为方向未知时 `in`/`out`/`net` 一律给 `null`、`total` 仍给可信合并值；pytest 103 passed、check 无 issue、容器内起真实链路逐个接口验过 | 部分采纳                  |
| 2026-11-17 | 梁林祎      | TraeCode（DeepSeek-V4.1-Flash）       | 编码 | 契约 v1.4：新增 `metric_quality` 表（`ts` 唯一，`late_count`/`invalid_count` 两列）与 `0003_metricquality` 迁移；`/api/summary/today` 的 `quality` 改为从该表读取、补上契约 4.4 一直缺的 `invalid_count`；契约升 v1.4 并新增 3.3 节 | 人工确认「非法事件可能没有 `station_id`，摊到每站会重复计数」是加独立表的真正理由，据此否决把列加进 `metric_window`；实测发现新表为空时 `late_count` 从 3 掉到 0，要求加过渡期回退——新表有值用新表，为空则回退 `metric_window` 逐站累加，`invalid_count` 无站点级来源可回退故给 0；补 3 个用例覆盖有值/回退/降级三条分支；复核时发现 `MetricWindow.ts` 的 help_text 原写「窗口结束时间」与契约 3.2「窗口起点」矛盾（consumer 实际写 `window_start`），趁计算端尚未照此写入一并改正，只动注释不动库结构，并入 0003 不另开迁移；pytest 118→122 passed、check 无 issue、迁移可回滚重放、实测 `quality` 三字段齐全且 `late_count` 恢复为 3 | 部分采纳（回退方案为人工追加） |
| 2026-11-17 | 林世韬 | TraeCode（DeepSeek-V4.1-Flash） | 编码 | 按分工看板迭代采集模块：stations.json 补站点中文名、线序、经纬度与线路名称/颜色；模拟器新增 --scenario 四场景与 --backfill 历史回填；消息 6 字段不变 | 人工复核发现 AI 最初把站点表改成 lines+stations 嵌套，服务端 /api/stations 会把这两个 key 当站点、按线路筛选随之全失效，已改回契约 4.5 的扁平结构并补齐 line_name/color/lng/lat；场景比例沿用早晚高峰 78%/72%，burst 单笔上限 8；与计算对齐后回填由消费端 --replay 按 event_time 分桶落库；pytest 109 passed、manage.py check 无 issue、/api/stations 实测返回 6 站 2 线、--scenario 与 --backfill dry-run 实测 | 直接采用 |
| 2026-11-17 | 林世韬 | TraeCode（DeepSeek-V4.1-Flash） | 编码 | 按服务端联调问题清单把 stations.json 改为 lines+stations 嵌套结构：线路名/颜色/顺序放 lines，站点放中文名/线序/经纬度/采样权重；simulator.py 改嵌套读取并加 load_network 结构校验；补嵌套结构、缺字段、线路顺序测试 | 人工拍板采纳清单方向（保留嵌套、通知服务端按新结构读，服务端 api/stations.py 由服务同学改）；缺字段容错口径人工定：缺 weight 默认 1.0 参与采样、缺 line_id 跳过该站并打印警告、顶层缺 lines/stations 直接报错退出；pytest 120 passed、manage.py check 无 issue、--dry-run --count 3 实测正常 | 直接采用 |
| 2026-11-17 | 兰天 | TraeCode | 编码 | 契约 v1.3 Slice 1：metric_window 加 in_passengers/out_passengers 两列，aggregate_window 按 event_type 拆分进/出聚合，consumer 双写落库，补 4 个 v1.3 测试 | 人工核对口径：passengers = in + out 一致性、缺 event_type 按进站兜底、只加不删（旧字段旧测试全保留）；pytest 62 passed、check 无 issue | 直接采用 |
| 2026-11-17 | 兰天 | TraeCode | 编码 | 解决采集 backfill 阻塞点：consumer 加 --replay 开关跳过迟到判定（方案 D）；采集队友反馈后修复——按 event_time 分桶逐窗落库、replay 不写 SNAPSHOT_KEY | 否决方案 A（放宽 late_threshold=2s 会污染实时口径，且阈值是指标口径 §4 定的，改它要先改文档）；首版只加 ignore_late，未解决 ts 堆 now 和写快照冲大屏，按队友反馈把 run_window 拆出 replay 路径重写；pytest 64→67 passed | 部分采纳（首版缺分桶与快照规避，经队友反馈修正） |
| 2026-11-17 | 兰天 | TraeCode | 编码 | 与 feat/contract-v1.3 撞车去重：撤销本分支 api/models.py 改动、删除重复的 0002 进/出列 migration | 用户拍板"不改队友内容、撤自己重复改动"；进/出列与 migration 的归属让给服务端，本分支只留 stream/ 与测试；pytest 67 passed、api/ diff 清空 | 直接采用 |
| 2026-11-17 | 兰天 | TraeCode | 文档 | 写 PR #17 评论：说明撞车已解决 + replay 修复内容与用法，经 gh CLI 发出 | 人工核对 diff --stat 与测试结果后定稿 | 直接采用 |
| 2026-11-24 | xuzhaopu | CodeBuddy Code（DeepSeek-V4.1-Flash） | 编码 | 修复「客流查询切换线路后全空」：定位为 api/stations.py 把采集端嵌套 stations.json 的顶层当站点表，build_line_map 恒空；改为读取 stations 子层、线路名与颜色取顶层 lines，并补一条嵌套结构回归测试 | 人工核对契约 4.5 字段未变、前端无需改动；复现命令与修复后 curl 对比、pytest 127 passed、manage.py check 无 issue、make doctor 11 项全绿、1 号线查询页截图确认有图有明细 | 直接采用 |
| 2026-11-24 | xuzhaopu | CodeBuddy Code（DeepSeek-V4.1-Flash） | 编码 | 修复「查询页选全部线路后仍为空」：查询页复用了实时页的 lineOptions，全部项的值是 all，setLine 把它当线路编码发出，后端按不存在的线路筛出空集；查询页改为独立的 queryLineOptions（全部用空串），并兼容旧链接里残留的 all | 人工核对两页语义差异与 hash 读写路径；line_id=all 旧链接截图确认有图、无 line_id 请求返回 297 点、pytest 127 passed、make doctor 全绿 | 直接采用 |
| 2026-11-24 | xuzhaopu | CodeBuddy Code（DeepSeek-V4.1-Flash） | 文档 | 按项目代码与真实运行结果写报告第三部分：启动 MySQL/Redis/消费者/模拟器/Django，截取采集、计算、服务、前端共 9 张图，写入“三、系统原型构建与实现”并插入 9 张图与 8 段代码 | 人工核对截图数值、接口字段与图号 3-1—3-9 的引用关系；逐句复核尚未完成 | 待人工复核 |
| 2026-11-24 | xuzhaopu | CodeBuddy Code（DeepSeek-V4.1-Flash） | 文档 | 按《实验报告文字风格指南》统一版式：38 个 Word 标题样式改为纯黑加粗段落（黑体），10 个表题改 10.5pt 加粗，18 张整版图统一 15.6cm；第三部分 9 处“见图 3-x”改“如下：”，38 个长段拆到 60 字内 | 核对第三部分以外文字逐段未变、23 图 10 表未动、标题颜色与字号；拆分后的断句尚未逐句复核 | 待人工复核 |
| 2026-11-24 | xuzhaopu | CodeBuddy Code（DeepSeek-V4.1-Flash） | 测试/编码 | 对实验做缺陷与有效性测试：只读核对真实 Redis/MySQL，定位并修复无时区时间崩溃、deferred 提前 ACK、落库失败仍 ACK、direction 改写 total、输入校验过宽、metric_quality 未写入、ignore_conflicts 丢更新等；新增固定消费者名与启动补消费，补回归用例，pytest 127→147 | 待人工逐条复核：契约 v1.5 的 total/写入策略、MySQL update_conflicts 无 unique_fields 分支、固定消费者名的单实例假设、补消费遇到迟到事件的处置；真实 MySQL/Redis 用隔离库验证后已 DROP/删键，测试阶段未动实验报告 | 待人工复核 |
| 2026-11-24 | xuzhaopu | CodeBuddy Code（DeepSeek-V4.1-Flash） | 文档 | 写报告第四部分并按第四部分统一第三部分口径：组件测试用 direction_sums 5 条路径，功能测试用等价类覆盖 5 组；第三部分 3-3/3-8 数字按截图重写，验收线改为 1.2 计划口径并注明未做连续压测；aggregate_window/run_window/int_param 三个代码片段替换为当前源码 | 待人工逐句复核：测试时间与测试人、P2 统计口径、2 秒与 10 分钟未实测的表述；改动第三部分 9 个段落与第四部分，未改第五部分 | 待人工复核 |
| 2026-11-24 | xuzhaopu | CodeBuddy Code（DeepSeek-V4.1-Flash） | 测试/文档 | 核对报告第四部分：实测 direction_sums 五条路径，比对表 4-2 的 37、minutes 1/60、290/273/233、12、26 与用例/截图；修正 4.4 的 P2 统计为“修复 5、缓解 1、文档澄清 1、剩余 4” | 147 用例与 manage.py check 重跑通过；表 4-2 的 minutes=1/60 为手工实测，自动化未单独覆盖 1 | 待人工复核 |
| 2026-11-24 | xuzhaopu | CodeBuddy Code（DeepSeek-V4.1-Flash） | 文档 | 补报告第一部分 1.2：新增不编号的用户故事清单（13 条，含角色、验收要点、对应模块），模板表1-1 角色列表、表1-2 用例列表保持原号；图 1-1 前补横轴主干活动与纵轴交付批次说明，图后按故事地图改写三批故事分布；Sprint 1 活动图数量由 3 张改 2 张 | 人工指出模板题号不重编，按模板恢复表1-1/1-2、故事清单不占编号；待逐条复核 13 条故事与 docs/01-需求/用户故事地图.md 一致、全文引用与表格跨页版式 | 待人工复核 |

## 填写说明

- **工具**：写具体名字（Cursor / Copilot / 通义灵码 / 文心快码 / ChatGPT），不要只写"AI"。
- **环节**：需求分析 / 系统设计 / 编码 / 测试 / 文档。
- **让它做了什么**：一句话说清任务，不写"帮我写代码"。
- **人工改了什么**：评分重点，写具体动作——补了哪个分支、换了哪个字段、删了哪段、为什么删。
- **采纳程度**：直接采用 / 部分采纳 / 仅参考 / 否决。

## 典型 Prompt 留档

挑 1—2 个效果最好或最失败的，把完整 Prompt 和 AI 输出摘要贴在这里。下面是实际用过的一条。

实际使用（2026-11-13，编码环节）：

```
背景：大屏页面 web/index.html，Vue 3 + ECharts 5 走 CDN，
      数据来自 /api/snapshot、/api/series、/api/rank（契约 v1.2）。
任务：保持现有深色风格，提升信息量：线网站点分布、柱子环比增减、
      排行点击下钻联动单站趋势、迟到/非法合并为数据健康走势、
      告警持续时长与峰值、页面宽度随视口自适应。
约束：只改 web/index.html；不改 api/ 与契约字段；
      1 秒快照 / 5 秒历史轮询、断线降级、空状态逻辑保持不变。
输出：先说明改哪几块、每块用哪个已有字段，再给完整文件。
```

被否决的那条也要留，写明否决理由（字段名对不上契约、引入了课程不允许的依赖、把窗口值算成了累计值等），报告"教训"那段直接用。

被否决的方案（2026-10-27，编码环节）：

- 用 python-dotenv 加载：requirements.txt 是共享文件，加依赖要全组同意，否决。
- 用 os.environ.update() 或直接赋值：会让 .env 压过 compose 注入的  
  MYSQL_HOST=mysql、REDIS_HOST=redis，容器模式连不上库，否决。
- 最终采用 config/env.py + os.environ.setdefault。

被否决的方案（2026-11-13，编码环节）：

- 整体换视觉风格：按"浅色值班风 / 霓虹科技风 / 瑞士极简风"各出了 1920×1080 效果图，  
  评审后否决——值班室长时间盯屏和投影亮度下原深色方案更稳，且换风格会让已验收的页面视觉断层。  
  结论：风格比选只作参考，不替换原方案。
- 帮助抽屉首版：5 个标签 + 角色速查 + 链路节点卡 + 页面地图 + 命令表 + 6 条 FAQ，  
  信息密度过高、与首屏职责重复，否决，压成 3 个标签、2 张表、4 条 FAQ。

实际使用（2026-11-24，编码环节）：

```
背景：大屏页面已经能用，但"AI 味"重：指标前的小圆点、品牌图标、眉标小字、
      排行名次的彩色块、卡片投影、9.5—11px 的一堆小字号、同一句话写两遍
      （标题说"近 5 分钟排行"，卡底再写一遍）。
任务：按"只删不加"的原则去装饰，并把 78KB 的单文件按功能拆开。
约束：不改接口与契约字段、不加新的运行时依赖（仍走 CDN，不引构建链）、
      1 秒快照 / 5 秒历史、断线降级、空状态逻辑一律不动；
      每个文件必须过 .githooks/pre-commit 的 20000 字节线。
输出：先列清单说明删哪几条、拆成哪几个文件，再给代码。
```

改动落在三处，都能在 `git diff` 里逐条对：`web/`（拆成 index.html + partials + assets）、`config/settings.py`（STATIC_URL 改绝对路径并加 STATICFILES_DIRS）、`DESIGN.md`（把这次的判断写成约定）。

被否决的方案（2026-11-24，编码环节）：

- 引入 Node/Vite 构建链拆文件：能让单文件变小，但违背"前端 CDN 引入、不引构建链"的组内约定，否决。
- 引 Lucide/Tabler 图标库：本页其实只有 ×、+、− 三个符号，直接删掉手画图标就够，多一个 CDN 依赖不值，否决。
- 保留单文件、把钩子阈值调高：等于把"agent 读不动"的问题藏起来，且违反 AGENTS 里"照报错拆文件，不要绕过钩子"，否决。
- 换成深色或另一套配色：值班现场已按浅色验收，换风格会让页面视觉断层（同 11-13 的结论），这次只做减法不动配色，否决。
