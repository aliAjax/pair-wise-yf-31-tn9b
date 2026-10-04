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
- `POST /api/recovery-plans`：一次提交方案及航班调整；保存即按航段时段占用飞机/机组（可带 `lease_ttl_seconds`，默认 120s，最大 3600s；可带 `takeover_target_id` 登记接管意图，租约为 `intended`，不真正占用目标资源）。
- `POST /api/plans/{id}/assignments`：用 `expected_revision` 临时改派，重新占用资源并续约。
- `POST /api/plans/{id}/leases/renew`、`/leases/release`、`POST /api/plans/{id}/leases`：续约、主动释放、重新获取（窗口失效后用它重新竞争并恢复 draft）。
- `GET /api/leases`：查看全部租约（含 released/superseded 历史与 `valid` 标记）；方案详情也带 `leases` 与 `window_stale`。
- `POST /api/disruptions/{id}/window`：用 `expected_revision` 乐观锁更新中断窗口；draft 方案立即置 `stale`、释放租约、按新窗口顺延航段并重算延误，返回 `invalidated_plans`；已锁定方案保留 `window_stale` 标记。
- `POST /api/plans/{id}/validate`、`/lock`：校验并原子锁定方案（锁定要求持有未过期的 held 租约，冲突信息带占用方 `occupant` 与 `free_candidates`；锁定后租约转为永久）。
- `POST /api/plans/{id}/takeover`：仅运行经理；`target_plan_id` 指向已锁定方案。按最新窗口重算延误、校验、原子移交租约（目标 held→`superseded`，本方案 intended→held）；写入失败自动回滚到原租约并重试（最多 3 次），成功响应含 `attempts`。
- `GET /api/takeovers`、`GET /api/disruptions/{id}/takeovers`：接管差异留存（新旧资源、时刻、逐航段延误与总延误差、窗口版本），首页控制台直接展示。
- `GET /api/disruptions/{id}/compare`：比较恢复方案成本。
- `POST /api/flights/{id}/cancel`、`/recover`：取消和人工恢复。
- `GET /api/state`、`GET /api/plans/{id}`：查询状态和影响（state 含 `active_leases` 与 `takeovers`）。

## 租约语义

同一资源（飞机或机组）在重叠时段最多只有一张有效 `held` 租约；`intended` 仅用于声明接管意图，不占用资源；租约带 TTL，可续约，过期后锁定/续约报 `lease_conflict`/`lease_lost` 并给出占用方和空闲候选。窗口更新会释放所有受影响 draft 方案的租约。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

时区和机场本地时刻没有引入完整时区数据库；模型使用简化航线许可与宵禁规则。身份头、SQLite 和单进程 HTTP 服务适合原型演示，正式运行需要外部身份系统、共享数据库和更强的跨实例锁。
