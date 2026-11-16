# 展品体验事件湖

本项目使用 FastAPI 和 SQLite 管理场馆、展品、来源契约与事件批次。原始事件保留接收证据，正式报表只读取已发布的日期快照。

## 展品迁移与身份映射

同一展品可先在 A 馆试展、后转至 B 馆，而无需新建展品 ID：

- `POST /exhibits/{exhibit_id}/migrations` 声明有生效日的迁移。迁移以**目标馆时区的本地日零点**为切换时刻；此前的事件仍记在来源馆，此后的事件记在目标馆，两段驻留区间首尾相接、互不重叠。
- 事件入湖时逐条按其时刻解析场馆与本地日，因此跨境、跨时区事件不会在两馆分区重复计数。
- 来源契约按馆绑定：迁移后沿用旧馆来源会得到 `outside_source_venue` 拒收，而不是悄悄写入错误分区。
- `POST /exhibits/{exhibit_id}/aliases` 把各馆沿用的本地编号映射到同一展品；别名只在生效窗口内解析，窗口外的历史查询仍返回其原始身份。
- `GET /exhibits/{exhibit_id}/conflicts` 汇总重叠驻留、同日双馆未发布事件与撤回后的残留；`GET /venues/{venue_id}/integrity` 也会报告迁移布局问题。
- `POST /migrations/{migration_id}/retract` 撤回迁移：**已发布快照与其事件归属保持原样**，未发布事件按恢复后的布局重新归属或明确列为需重放。
- 历史不会被重新解释：迁移前已发布的快照仍按原场馆、原日期返回；若来源馆在迁移日之后已有发布数据，追溯迁移会被拒绝。

## 运行

在预装依赖的环境中，从项目根目录执行：

    uvicorn exhibit_lake.api:app --host 0.0.0.0 --port 8000

默认数据库为本地 SQLite 文件；用 EXHIBIT_LAKE_DB 可指定其他路径。

## 测试与构建

    python -m unittest discover -s tests -v
    python -m compileall -q exhibit_lake
    python -m exhibit_lake.demo

以上三条命令依次运行业务测试、编译检查和本地合成数据验收。依赖版本记录在 requirements.txt。
