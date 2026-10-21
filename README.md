# 实时客流监测大屏

地铁闸机刷卡数据 -> 实时计算各站客流 -> 大屏动态展示。软工实验第 9 选题（实时数据流处理与可视化系统）。

技术栈：Django 4.2 + MySQL 8 + Redis 7 + Vue 3 + ECharts 5，前端用 CDN，不引入 Node 构建链。

---

## 一、跑起来

```bash
# 1. 关掉依赖：只起 MySQL 和 Redis，Web 在本机跑，方便看日志
docker compose up -d mysql redis

# 2. Python 环境（3.11 以上）
python -m venv .venv
source .venv/Scripts/activate      # Windows Git Bash；macOS/Linux 用 source .venv/bin/activate
pip install -r requirements.txt

# 3. 建表
python manage.py migrate

# 4. 三个终端各跑一个
python stream/consumer.py         # 计算：读事件 -> 聚合 -> 写快照与历史
python simulator/simulator.py     # 采集：造数据写进 Redis Stream
python manage.py runserver 8000   # 服务：打开 http://127.0.0.1:8000
```

或者一条命令 `bash scripts/dev_up.sh`（依赖容器 + 迁移 + 三个进程一起起）。

想改密码、端口、窗口长度，`cp .env.example .env` 后改 `.env`，Django 和 compose 都读它。默认值只够本地开发。

## 二、能看到什么

- 顶部实时客流数字与告警横幅（单窗口超过 `ALERT_THRESHOLD` 触发，默认 100 人次/秒）
- 各站客流柱状图，可按 1 号线 / 2 号线筛选
- 近 5 分钟客流排行与趋势折线
- 底部显示数据时间、迟到条数、非法条数、连接状态

想验证异常处理，让模拟器掺点脏数据：

```bash
python simulator/simulator.py --inject 0.1
```
非法条数会涨，页面不会崩，消费者也不会退出。

想演示历史与回放，直接重放固定样例，结果可复现：

```bash
python simulator/simulator.py --replay data/samples/sample_events.json
```

## 三、目录与分工

| 目录 | 职责 | 负责 |
|------|------|------|
| `simulator/` | 造事件、写 Stream，支持限速、限量、异常注入、历史回放 | 采集 |
| `stream/` | 消费组读取、窗口聚合、写快照、落库 | 计算 |
| `api/` | 模型、快照/序列/排行接口、页面渲染 | 服务 |
| `web/` | 大屏页面（Vue 3 + ECharts） | 前端 |
| `config/` | Django 配置与环境变量 | 服务 |
| `tests/` | 单元测试与接口测试，不需要真数据库和 Redis | 各模块 |
| `docs/` | 需求、设计、建模、测试、项目管理 | 文档 |

只改自己负责的目录。文件级责任划分、四个跨目录接口点的归属、改别人目录的处理方式，见 `docs/05-管理/分工与责任矩阵.md`。

```
.
├── config/           Django 配置（settings / urls / wsgi / 测试配置）
├── api/              应用：模型、视图、路由、迁移
├── simulator/        数据模拟器
├── stream/           消费者与指标计算
├── web/              index.html
├── tests/            pytest 用例
├── scripts/          本地启动脚本
├── data/samples/     固定样例数据（进 Git）
├── docs/             01-需求 / 02-设计 / 03-建模 / 04-测试 / 05-管理
└── .github/          PR 与任务卡模板
```

## 四、接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 大屏页面 |
| GET | `/api/snapshot` | 最新 1 秒快照，无数据时返回空结构而不是 404 |
| GET | `/api/series?station_id=&minutes=5` | 历史序列，minutes 取 1—60 |
| GET | `/api/rank?top=5` | 最近 5 分钟客流排行 |
| GET | `/api/alert` | 客流告警，阈值由 `ALERT_THRESHOLD` 配置，判定在服务端 |

字段、口径、错误码见 `docs/02-设计/接口契约.md`；指标怎么算见 `docs/02-设计/指标口径.md`。改接口先改文档。

## 五、测试

```bash
pytest          # 30 个用例，用 SQLite 与失效端口，不需要起 MySQL/Redis
```

## 六、常见问题

| 现象 | 原因与处理 |
|------|-----------|
| 页面显示"暂无数据" | 消费者或模拟器没起；先 `redis-cli GET metric:snapshot` 看有没有快照 |
| `Access denied for user 'root'` | `.env` 里的密码和容器实际密码不一致，改完 `docker compose down -v` 重建 |
| `cryptography package is required` | 少装了 `cryptography`，重新 `pip install -r requirements.txt` |
| 端口 8000 被占用 | 大概率是 compose 里的 web 容器还在跑，`docker compose stop web` |
| 数字不动但页面正常 | 快照过期了（TTL 5 秒），说明消费者已停止 |

## 七、当前进度

| 模块 | 已完成 | 下一步 |
|------|--------|--------|
| 采集 | 限速、限量、异常注入、历史回放、早晚高峰分布 | 站点表改为配置驱动 |
| 计算 | 消费组、窗口对齐、跨窗口事件不重复计入、按站点落库 | 迟到事件回补历史窗口 |
| 服务 | 快照 / 序列 / 排行 / 告警、参数校验、统一错误结构 | 告警推送（WebSocket） |
| 前端 | 柱状图 + 线路筛选 + 排行 + 趋势 + 断线提示 | 站点下钻、导出 |
| 测试 | 39 个用例（口径、切窗、解析、接口、告警） | 落库异常路径 |

迭代计划与任务分工见 `docs/05-管理/`。
