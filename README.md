# 航班中断恢复系统

独立的 Python 标准库项目，用 SQLite 保存机场、飞机、机组、航线许可、航班、中断事件和恢复方案。系统会校验维护间隔、执勤时限、机场宵禁、航线许可、资源重叠，并计算取消、延误、受影响旅客和错失衔接成本。

## 运行

```bash
python3 app.py --db airline_recovery.db
```

默认监听 `127.0.0.1:8202`，首页为 `/`，健康检查为 `/health`。

身份头：`X-User-Id`、`X-Role`。角色包括 `viewer`、`scheduler`、`ops_manager`、`auditor`。

## 主要接口

- `POST /api/airports`、`/api/aircraft`、`/api/crew`、`/api/permits`：基础资源与约束。
- `POST /api/flights`、`POST /api/disruptions`：创建航班和中断。
- `POST /api/recovery-plans`：一次提交方案及航班调整。
- `POST /api/plans/{id}/assignments`：用 `expected_revision` 临时改派。
- `POST /api/plans/{id}/validate`、`/lock`：校验并原子锁定方案。
- `GET /api/disruptions/{id}/compare`：比较恢复方案成本。
- `POST /api/flights/{id}/cancel`、`/recover`：取消和人工恢复。
- `GET /api/state`、`GET /api/plans/{id}`：查询状态和影响。

## 可续期资源租约

保存调整（`POST /api/recovery-plans`、`POST /api/plans/{id}/assignments`）时，系统按航段时段为飞机和机组占用资源租约。重叠时段只允许一张有效租约；晚到的保存会收到 `lease_conflict`，详情给出占用方（`occupant`）和同一时段空闲的同类候选（`candidates`）。

- `POST /api/plans/{id}/renew`：续期方案下全部有效租约（延长 `expires_at`）。
- `GET /api/plans/{id}/leases`：查看方案的租约与过期状态。
- 租约过期后不再占用资源，其它方案可在该时段建立新租约。有效期由环境变量 `AIRLINE_LEASE_SECONDS` 配置，默认 600 秒。

## 中断窗口更新与方案接管

- `POST /api/disruptions/{id}/window`：更新中断窗口。旧方案立即失效（状态置为 `stale`）并释放租约，等待重算或接管。
- `POST /api/plans/{id}/takeover`：运行经理让接管方案接手已锁定方案，按最新窗口重算延误并接管租约。写入失败时事务回滚、回到原租约重试；接管差异（`diff`）写入审计日志，控制台可查。请求体可带 `assignments` 覆盖部分航段的飞机、机组或时刻。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

时区和机场本地时刻没有引入完整时区数据库；模型使用简化航线许可与宵禁规则。身份头、SQLite 和单进程 HTTP 服务适合原型演示，正式运行需要外部身份系统、共享数据库和更强的跨实例锁。
