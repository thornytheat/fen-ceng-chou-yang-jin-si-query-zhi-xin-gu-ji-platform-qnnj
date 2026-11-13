# migrations

`0001_initial.sql` 是 MySQL 8.0 的表结构基线，与 `src/models/*` 的 SQLAlchemy 定义一一对应。
容器启动时 `src/config/app.py` 先执行 `Base.metadata.create_all()`（幂等建表），
因此空库可直接起服务；本目录的 SQL 供 DBA 手工建库 / 审阅使用。

`0002_data_snapshots.sql` 引入不可变数据快照：新增 `data_snapshots` 表，并给
`datasets / sample_plans / approx_queries` 加快照关联。脚本幂等（`IF NOT EXISTS` +
information_schema 判断），并会为存量数据集回填 v1 基线快照、回填方案与查询的快照绑定。
正常升级无需手工执行——应用启动时 `create_all()` 建表、`_ensure_baseline_snapshots()` 补基线；
该 SQL 仅用于不走 ORM 自动建表的 DBA 流程。

重新初始化：

```bash
docker compose exec -T db mysql -u"$DB_USER" -p"$DB_PASSWORD" "$DB_NAME" < migrations/0001_initial.sql
docker compose exec -T db mysql -u"$DB_USER" -p"$DB_PASSWORD" "$DB_NAME" < migrations/0002_data_snapshots.sql
```
