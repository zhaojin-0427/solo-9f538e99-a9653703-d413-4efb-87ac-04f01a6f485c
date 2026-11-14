# 数据血缘影响分析 API（FastAPI + SQLite）

本地部署的血缘影响分析服务：批量登记**源字段 / 派生字段 / 报表列**的依赖关系，
提交拟下线字段后，递归计算**确实失效的报表列**，并给出每个失效列各候选输入组
缺失的具体依赖。

## 语义规则

- 每个下游列可登记**多个候选输入组**（`candidate_groups`，组的数组）。
- 一个候选组可用 ⇔ 组内字段**全部**可用。
- 一个列可产出 ⇔ **任一**候选组可用；仍有可用组的列不会被误报。
- 源字段（`source`）没有输入；派生 / 报表列必须至少有一个非空候选组。
- 提交拟下线字段后沿血缘图递归传播：
  - 只有所有候选组都不可用的列才计入失效；
  - 返回失效报表列的**每一个**失效候选组及组内缺失字段；
  - 每个缺失字段附带 `root_causes`（追溯到最深层的拟下线字段，而非中间节点）。
- 登记批次若引入循环依赖（含自环），**整批回滚不生效**。
- 源字段可**更名**：旧名、新名一次提交，源字段及所有候选组引用在同一事务内迁移；
  旧名转为**停用名**（不可再登记或引用），以新名做下线分析可延续更名前的失效关系。

## 目录结构

```
lineage-api/
├── app/
│   ├── main.py         # FastAPI 入口、异常映射、生命周期
│   ├── service.py      # 整批事务登记、源字段更名、循环检测、递归失效分析
│   ├── db.py           # SQLite 连接 / WAL / 快照与写事务
│   ├── models.py       # Pydantic 请求与响应模型
│   └── exceptions.py   # 领域错误（422/409/404）
├── tests/              # pytest：功能用例 + 真实 uvicorn 并发用例
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── examples/           # 可直接调用的请求样例
```

## Docker Compose 启动

```bash
cd lineage-api
docker compose up -d --build
docker compose ps          # health: healthy
curl http://localhost:8000/health
```

- 服务监听 `8000`，SQLite 数据库落在命名卷 `lineage-data` 的
  `/data/lineage.db`（WAL 模式），容器重建后数据保留。
- 交互式文档：http://localhost:8000/docs （OpenAPI 3.1）。
- 停止与清理：

```bash
docker compose down          # 停止，保留数据卷
docker compose down -v       # 同时删除数据卷
```

## 本地（无 Docker）运行

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
LINEAGE_DB_PATH=./lineage.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API 入口

### 1. 批量登记血缘 `POST /lineage/register`

同一批次内的节点可以互相引用；重复登记同名节点会**整体替换**其候选组。
任何校验失败、类型冲突或循环依赖都返回错误且整批不落库。

```bash
curl -s -X POST http://localhost:8000/lineage/register \
  -H 'Content-Type: application/json' \
  -d @examples/register.json
```

请求体：

```json
{
  "nodes": [
    {"name": "order_amt", "kind": "source"},
    {"name": "coupon_amt", "kind": "source"},
    {"name": "net_amt", "kind": "derived",
     "candidate_groups": [["order_amt", "coupon_amt"]]},
    {"name": "report_gmv", "kind": "report",
     "candidate_groups": [["net_amt"], ["order_amt"]]}
  ]
}
```

错误约定：

| HTTP | error                     | 场景                                       |
|------|---------------------------|--------------------------------------------|
| 422  | `validation_error`        | 空候选组、源字段带输入、引用未登记字段等   |
| 409  | `cycle_detected`          | 批次将引入循环依赖，整批回滚（附 cycle 路径）|
| 409  | `conflict`                | 同名节点已存在但类型不同                   |
| 409  | `retired_name`            | 批次创建或引用了已停用名（更名退役的旧名） |

### 2. 源字段更名 `POST /lineage/rename`

上游源字段改名时，提交旧名、新名，**一次迁移**源字段及派生列 / 报表列
候选输入组中的全部引用（同一事务内完成）：

```bash
curl -s -X POST http://localhost:8000/lineage/rename \
  -H 'Content-Type: application/json' \
  -d @examples/rename.json
```

```json
{
  "old_name": "order_amt",
  "new_name": "order_amount"
}
```

响应：

```json
{
  "old_name": "order_amt",
  "new_name": "order_amount",
  "updated_references": 2,
  "message": "源字段已更名，全部引用已迁移，旧名已停用"
}
```

更名规则：

- 旧名随即转为**停用名**：后续登记批次若**创建**同名节点或在候选组中
  **引用**它，整批拒绝（`409 retired_name`，details 附 `renamed_to` 指引新名）；
  旧名不会回流进血缘图。
- 以下情况更名**不改变血缘**，返回错误：
  - 旧名不存在（含已停用）：`404 not_found`；
  - 旧名不是源字段（派生 / 报表列）：`409 conflict`；
  - 新名已被占用或新名本身是停用名：`409 conflict`；
  - 新旧名相同：`422 validation_error`。
- 更名与批量登记共用同一写锁串行执行：查询只会看到完整的更名前态或后态，
  不会残留旧名引用。
- 以**新名**做下线影响分析，失效报表列、缺失依赖与根因关系与更名前
  以旧名查询一致（仅字段名替换为新名）；以旧名查询返回 `404`，
  details 中的 `renamed` 会提示其已更名为哪个字段。
- 当前停用名列表可通过 `GET /lineage/graph` 的 `retired_names` 查看。

### 3. 下线影响分析 `POST /lineage/impact`

```bash
curl -s -X POST http://localhost:8000/lineage/impact \
  -H 'Content-Type: application/json' \
  -d '{"retiring": ["coupon_amt"]}'
```

响应（节选）：

```json
{
  "retiring": ["coupon_amt"],
  "failed_report_columns": [
    {
      "name": "report_coupon_rate",
      "failed_groups": [
        {
          "group_index": 0,
          "inputs": ["coupon_amt", "order_amt"],
          "missing": [
            {"name": "coupon_amt", "kind": "source", "root_causes": ["coupon_amt"]}
          ]
        }
      ]
    }
  ],
  "unavailable_count": 3,
  "unavailable_nodes": ["coupon_amt", "net_amt", "report_coupon_rate"]
}
```

- `failed_report_columns` 只包含**所有候选组均失效**的报表列；
  仍有可用组（如上例中的 `report_gmv`，备用组 `[order_amt]` 可用）不会出现。
- `unavailable_nodes` 列出全部不可用节点（含中间派生列），方便逐级排查。
- 拟下线字段未登记时返回 `404 not_found`；若该名是更名后的停用旧名，
  响应 details 的 `renamed` 会给出其新名。

### 4. 查看当前血缘 `GET /lineage/graph`

返回最近一次已提交的完整血缘（节点类型 + 全部候选组）以及
`retired_names`（更名退役的停用名及其对应新名）。

### 5. 健康检查 `GET /health`

## 并发与一致性设计

1. **WAL 模式**：读写不互相阻塞。
2. **登记 / 更名串行化**：每个登记批次与每次源字段更名都使用
   `BEGIN IMMEDIATE` 先拿写锁，并发写操作排队执行，
   配合 `busy_timeout` 避免 `database is locked`。
3. **查询快照隔离**：每次影响分析 / 血缘查询在单连接显式事务内完成，
   SQLite 的快照读保证请求只看到**登记 / 更名前或后**的完整血缘，
   不可能读到应用了一半的批次或残留旧名引用的中间态。
4. **整批原子性**：节点与候选组分两阶段写入（先全部节点、再全部边），
   写入后做全图 DFS 环检测，发现环则抛错触发 `ROLLBACK`，批次内任何数据都不留存。
5. **停用名防回流**：更名把旧名写入 `retired_names`（与引用迁移同一事务），
   登记校验在同一写锁内检查停用名，因此旧名要么尚未退役、要么永久拒绝，
   不存在并发窗口让旧名重新进入血缘图。

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

覆盖：候选组 AND / OR 语义、递归失效与根因追溯、备用组存活不误报、
循环 / 自环整批回滚、各类 422/409/404、重复登记替换、
源字段更名（引用迁移、停用名拒绝创建 / 引用、链式更名、
更名前后失效关系延续、失败更名零副作用），
以及在真实 uvicorn 服务上的并发读写与并发「更名 vs 登记」
（读者只观测到完整快照，旧名绝不回流）。
