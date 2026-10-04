#!/usr/bin/env python3
"""Airline disruption recovery engine using standard-library SQLite and HTTP."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from time import sleep as sleep_seconds
from urllib.parse import parse_qs, urlparse

PORT = 8202
ROLES = {"viewer", "scheduler", "ops_manager", "auditor"}
DEFAULT_LEASE_TTL_SECONDS = 120
MAX_LEASE_TTL_SECONDS = 3600
TAKEOVER_WRITE_ATTEMPTS = 3
TAKEOVER_RETRY_BACKOFF = 0.01


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or utcnow()).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime:
    if not value:
        raise ApiError(400, "time_required", "必须提供 ISO 8601 时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError(400, "invalid_time", f"时间格式错误: {value}") from exc
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def parse_clock(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ApiError(400, "invalid_clock", f"时刻格式应为 HH:MM: {value}") from exc


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


class Repository:
    def __init__(self, db_path: str | Path):
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        # 测试钩子：剩余多少次提交强制失败；事务回滚后的回调（用于验证租约回滚）
        self.fail_commits_remaining = 0
        self.on_rollback = None
        self._init()

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            if self.fail_commits_remaining > 0:
                self.fail_commits_remaining -= 1
                raise sqlite3.OperationalError("injected commit failure")
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            if self.on_rollback is not None:
                self.on_rollback()
            raise

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}

    def _init(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS airports(code TEXT PRIMARY KEY, country TEXT NOT NULL, curfew_start TEXT NOT NULL, curfew_end TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS aircraft(id TEXT PRIMARY KEY, model TEXT NOT NULL, maintenance_due TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active');
            CREATE TABLE IF NOT EXISTS crew(id TEXT PRIMARY KEY, name TEXT NOT NULL, base TEXT NOT NULL, duty_start TEXT NOT NULL, max_duty_minutes INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'active');
            CREATE TABLE IF NOT EXISTS permits(id INTEGER PRIMARY KEY AUTOINCREMENT, origin TEXT NOT NULL, destination TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT NOT NULL, curfew_exempt INTEGER NOT NULL DEFAULT 0, UNIQUE(origin,destination,valid_from,valid_to));
            CREATE TABLE IF NOT EXISTS flights(
                id INTEGER PRIMARY KEY AUTOINCREMENT, flight_no TEXT NOT NULL UNIQUE, origin TEXT NOT NULL, destination TEXT NOT NULL,
                std TEXT NOT NULL, sta TEXT NOT NULL, aircraft_id TEXT NOT NULL REFERENCES aircraft(id), crew_id TEXT NOT NULL REFERENCES crew(id),
                passenger_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'scheduled', delay_minutes INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 1, cancel_reason TEXT, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS disruptions(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, resource_id TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS recovery_plans(
                id INTEGER PRIMARY KEY AUTOINCREMENT, disruption_id INTEGER NOT NULL REFERENCES disruptions(id), name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft', revision INTEGER NOT NULL DEFAULT 1, score_json TEXT, metrics_json TEXT,
                window_revision INTEGER NOT NULL DEFAULT 1, takeover_target_id INTEGER REFERENCES recovery_plans(id),
                created_by TEXT NOT NULL, created_at TEXT NOT NULL, locked_at TEXT, locked_by TEXT
            );
            CREATE TABLE IF NOT EXISTS assignments(
                id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES recovery_plans(id) ON DELETE CASCADE,
                flight_id INTEGER NOT NULL REFERENCES flights(id), aircraft_id TEXT NOT NULL REFERENCES aircraft(id), crew_id TEXT NOT NULL REFERENCES crew(id),
                orig_std TEXT, new_std TEXT NOT NULL, new_sta TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'planned', delay_minutes INTEGER NOT NULL DEFAULT 0,
                missed_connections INTEGER NOT NULL DEFAULT 0, UNIQUE(plan_id,flight_id)
            );
            CREATE TABLE IF NOT EXISTS resource_leases(
                id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES recovery_plans(id) ON DELETE CASCADE,
                assignment_id INTEGER REFERENCES assignments(id) ON DELETE CASCADE, resource_type TEXT NOT NULL CHECK(resource_type IN ('aircraft','crew')),
                resource_id TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'held' CHECK(state IN ('held','intended','released','superseded')),
                token TEXT NOT NULL UNIQUE, expires_at TEXT, created_at TEXT NOT NULL, renewed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_leases_resource ON resource_leases(resource_type,resource_id,state);
            CREATE TABLE IF NOT EXISTS takeovers(
                id INTEGER PRIMARY KEY AUTOINCREMENT, disruption_id INTEGER NOT NULL, taking_plan_id INTEGER NOT NULL REFERENCES recovery_plans(id),
                target_plan_id INTEGER NOT NULL REFERENCES recovery_plans(id), window_revision INTEGER NOT NULL, attempts INTEGER NOT NULL,
                diff_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, actor TEXT NOT NULL, role TEXT NOT NULL, action TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL);
            """
        )
        # 旧库幂等迁移
        if "orig_std" not in self._columns(self.conn, "assignments"):
            self.conn.execute("ALTER TABLE assignments ADD COLUMN orig_std TEXT")
        if "revision" not in self._columns(self.conn, "disruptions"):
            self.conn.execute("ALTER TABLE disruptions ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
        plan_cols = self._columns(self.conn, "recovery_plans")
        if "window_revision" not in plan_cols:
            self.conn.execute("ALTER TABLE recovery_plans ADD COLUMN window_revision INTEGER NOT NULL DEFAULT 1")
        if "takeover_target_id" not in plan_cols:
            self.conn.execute("ALTER TABLE recovery_plans ADD COLUMN takeover_target_id INTEGER")

    @staticmethod
    def audit(conn: sqlite3.Connection, plan_id: int | None, actor: str, role: str, action: str, detail: dict[str, Any]) -> None:
        conn.execute("INSERT INTO audit_log(plan_id,actor,role,action,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                     (plan_id, actor, role, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), iso()))


class AirlineRecoveryService:
    def __init__(self, db_path: str | Path):
        self.repo = Repository(db_path)

    @staticmethod
    def identity(headers: Any) -> tuple[str, str]:
        actor, role = headers.get("X-User-Id", "").strip(), headers.get("X-Role", "").strip()
        if not actor or role not in ROLES:
            raise ApiError(401, "unauthorized", "需要 X-User-Id 和有效 X-Role")
        return actor, role

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    def seed_airport(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "ops_manager":
            raise ApiError(403, "seed_forbidden", "只有运行经理可以维护机场数据")
        code = str(body.get("code", "")).upper().strip()
        country = str(body.get("country", "")).upper().strip()
        if not code or not country:
            raise ApiError(400, "missing_fields", "code 和 country 必填")
        start = str(body.get("curfew_start", "23:00")); end = str(body.get("curfew_end", "06:00"))
        parse_clock(start); parse_clock(end)
        with self.repo.tx() as conn:
            conn.execute("INSERT OR REPLACE INTO airports(code,country,curfew_start,curfew_end) VALUES(?,?,?,?)", (code, country, start, end))
            return dict(conn.execute("SELECT * FROM airports WHERE code=?", (code,)).fetchone())

    def seed_aircraft(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "ops_manager": raise ApiError(403, "seed_forbidden", "只有运行经理可以维护飞机")
        ident, model = str(body.get("id", "")).strip(), str(body.get("model", "")).strip()
        if not ident or not model: raise ApiError(400, "missing_fields", "id 和 model 必填")
        due = iso(parse_time(body.get("maintenance_due")))
        with self.repo.tx() as conn:
            conn.execute("INSERT OR REPLACE INTO aircraft(id,model,maintenance_due,status) VALUES(?,?,?,?)", (ident, model, due, body.get("status", "active")))
            return dict(conn.execute("SELECT * FROM aircraft WHERE id=?", (ident,)).fetchone())

    def seed_crew(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "ops_manager": raise ApiError(403, "seed_forbidden", "只有运行经理可以维护机组")
        ident, name, base = str(body.get("id", "")).strip(), str(body.get("name", "")).strip(), str(body.get("base", "")).upper().strip()
        duty = parse_time(body.get("duty_start")); maximum = body.get("max_duty_minutes")
        if not ident or not name or not base or not isinstance(maximum, int) or maximum <= 0:
            raise ApiError(400, "invalid_crew", "id、name、base 和正整数 max_duty_minutes 必填")
        with self.repo.tx() as conn:
            conn.execute("INSERT OR REPLACE INTO crew(id,name,base,duty_start,max_duty_minutes,status) VALUES(?,?,?,?,?,?)", (ident, name, base, iso(duty), maximum, body.get("status", "active")))
            return dict(conn.execute("SELECT * FROM crew WHERE id=?", (ident,)).fetchone())

    def create_permit(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "ops_manager": raise ApiError(403, "permit_forbidden", "只有运行经理可以维护航线许可")
        origin, destination = str(body.get("origin", "")).upper(), str(body.get("destination", "")).upper()
        if not origin or not destination: raise ApiError(400, "missing_fields", "origin 和 destination 必填")
        valid_from, valid_to = parse_time(body.get("valid_from")), parse_time(body.get("valid_to"))
        if valid_to <= valid_from: raise ApiError(400, "invalid_permit", "许可结束时间必须晚于开始时间")
        with self.repo.tx() as conn:
            try:
                cur = conn.execute("INSERT INTO permits(origin,destination,valid_from,valid_to,curfew_exempt) VALUES(?,?,?,?,?)",
                                   (origin, destination, iso(valid_from), iso(valid_to), int(bool(body.get("curfew_exempt")))))
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "permit_exists", "相同航线与有效期的许可已存在") from exc
            return dict(conn.execute("SELECT * FROM permits WHERE id=?", (cur.lastrowid,)).fetchone())

    def create_flight(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"scheduler", "ops_manager"}: raise ApiError(403, "flight_forbidden", "当前角色不能创建航班")
        required = ("flight_no", "origin", "destination", "std", "sta", "aircraft_id", "crew_id")
        if any(not body.get(k) for k in required): raise ApiError(400, "missing_fields", f"缺少字段: {', '.join(k for k in required if not body.get(k))}")
        std, sta = parse_time(body["std"]), parse_time(body["sta"])
        if sta <= std: raise ApiError(400, "invalid_times", "到达时间必须晚于起飞时间")
        passengers = body.get("passenger_count", 0)
        if not isinstance(passengers, int) or passengers < 0: raise ApiError(400, "invalid_passengers", "passenger_count 必须是非负整数")
        with self.repo.tx() as conn:
            for table, ident in (("aircraft", body["aircraft_id"]), ("crew", body["crew_id"])):
                row = conn.execute(f"SELECT status FROM {table} WHERE id=?", (ident,)).fetchone()
                if not row or row["status"] != "active": raise ApiError(409, "resource_unavailable", f"{table} {ident} 不可用")
            try:
                cur = conn.execute("""INSERT INTO flights(flight_no,origin,destination,std,sta,aircraft_id,crew_id,passenger_count,updated_at)
                                      VALUES(?,?,?,?,?,?,?,?,?)""",
                                   (body["flight_no"].upper(), body["origin"].upper(), body["destination"].upper(), iso(std), iso(sta),
                                    body["aircraft_id"], body["crew_id"], passengers, iso()))
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "flight_exists", "航班号已存在") from exc
            return dict(conn.execute("SELECT * FROM flights WHERE id=?", (cur.lastrowid,)).fetchone())

    def create_disruption(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"scheduler", "ops_manager"}: raise ApiError(403, "disruption_forbidden", "当前角色不能登记中断")
        kind, resource = str(body.get("kind", "")).strip(), str(body.get("resource_id", "")).strip()
        if kind not in {"airport_closure", "aircraft_fault", "crew_timeout"} or not resource:
            raise ApiError(400, "invalid_disruption", "kind 或 resource_id 无效")
        start, end = parse_time(body.get("starts_at")), parse_time(body.get("ends_at"))
        if end <= start: raise ApiError(400, "invalid_times", "中断结束时间必须晚于开始时间")
        with self.repo.tx() as conn:
            cur = conn.execute("INSERT INTO disruptions(kind,resource_id,starts_at,ends_at,created_at) VALUES(?,?,?,?,?)",
                               (kind, resource.upper() if kind == "airport_closure" else resource, iso(start), iso(end), iso()))
            return dict(conn.execute("SELECT * FROM disruptions WHERE id=?", (cur.lastrowid,)).fetchone())

    def create_plan(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"scheduler", "ops_manager"}: raise ApiError(403, "plan_forbidden", "当前角色不能创建恢复方案")
        disruption_id, name = body.get("disruption_id"), str(body.get("name", "")).strip()
        assignments = body.get("assignments", [])
        if not isinstance(disruption_id, int) or not name or not isinstance(assignments, list):
            raise ApiError(400, "invalid_plan", "disruption_id、name 和 assignments 必填")
        ttl = self._ttl(body)
        takeover_target_id = body.get("takeover_target_id")
        with self.repo.tx() as conn:
            disruption = conn.execute("SELECT * FROM disruptions WHERE id=?", (disruption_id,)).fetchone()
            if not disruption:
                raise ApiError(404, "disruption_not_found", "中断事件不存在")
            if takeover_target_id is not None:
                if not isinstance(takeover_target_id, int):
                    raise ApiError(400, "invalid_target", "takeover_target_id 必须是整数")
                target = conn.execute("SELECT * FROM recovery_plans WHERE id=?", (takeover_target_id,)).fetchone()
                if not target or target["status"] not in ("locked",):
                    raise ApiError(409, "target_not_locked", "接管目标必须是已锁定方案")
                if target["disruption_id"] != disruption_id:
                    raise ApiError(409, "target_mismatch", "接管目标必须针对同一中断事件")
            cur = conn.execute("""INSERT INTO recovery_plans(disruption_id,name,created_by,created_at,window_revision,takeover_target_id)
                                  VALUES(?,?,?,?,?,?)""",
                               (disruption_id, name, actor, iso(), disruption["revision"], takeover_target_id))
            plan_id = cur.lastrowid
            for item in assignments:
                self._insert_assignment(conn, plan_id, item, replace=False)
            # 保存即占用：接管意图用 intended 租约（不真正占用目标的资源），其余方案用 held 租约
            leases = self._acquire_leases(conn, plan_id, ttl, intended_target_id=takeover_target_id)
            Repository.audit(conn, plan_id, actor, role, "plan_created",
                             {"disruption_id": disruption_id, "assignment_count": len(assignments),
                              "leases": len(leases), "takeover_target_id": takeover_target_id})
            result = self.get_plan(plan_id)
            result["leases"] = leases
            result["takeover"] = {"target_plan_id": takeover_target_id,
                                  "notice": "已登记接管意图：租约为 intended，需运行经理执行 takeover 才会接手"} if takeover_target_id else None
            return result

    def _insert_assignment(self, conn: sqlite3.Connection, plan_id: int, item: dict[str, Any], replace: bool) -> None:
        required = ("flight_id", "aircraft_id", "crew_id", "new_std", "new_sta")
        if any(item.get(k) in (None, "") for k in required): raise ApiError(400, "invalid_assignment", f"飞行调整缺少字段: {', '.join(k for k in required if item.get(k) in (None, ''))}")
        plan = conn.execute("SELECT * FROM recovery_plans WHERE id=?", (plan_id,)).fetchone()
        if not plan: raise ApiError(404, "plan_not_found", "方案不存在")
        if plan["status"] != "draft": raise ApiError(409, "plan_locked", "已锁定方案不能修改")
        std, sta = parse_time(item["new_std"]), parse_time(item["new_sta"])
        if sta <= std: raise ApiError(400, "invalid_times", "新到达时间必须晚于新起飞时间")
        flight = conn.execute("SELECT * FROM flights WHERE id=?", (item["flight_id"],)).fetchone()
        if not flight: raise ApiError(404, "flight_not_found", "航班不存在")
        if flight["status"] == "canceled" and item.get("status", "planned") != "canceled":
            raise ApiError(409, "canceled_flight", "已取消航班不能安排执行")
        # 延误始终相对航班原始计划时刻；首次调整时把基准固化到 assignment。
        # 若航班已被同中断的更早（已锁定）方案改写，沿历史 assignment 找回原始基准。
        existing = conn.execute("SELECT orig_std FROM assignments WHERE plan_id=? AND flight_id=?",
                                (plan_id, item["flight_id"])).fetchone()
        if existing and existing["orig_std"]:
            orig_std_s = existing["orig_std"]
        else:
            orig_std_s = flight["std"]
            this_plan = conn.execute("SELECT disruption_id FROM recovery_plans WHERE id=?", (plan_id,)).fetchone()
            if this_plan:
                prior = conn.execute("""SELECT a.orig_std FROM assignments a JOIN recovery_plans p ON p.id=a.plan_id
                                        WHERE a.flight_id=? AND a.orig_std IS NOT NULL
                                        AND p.disruption_id=? AND p.id!=? ORDER BY a.id LIMIT 1""",
                                     (item["flight_id"], this_plan["disruption_id"], plan_id)).fetchone()
                if prior:
                    orig_std_s = prior["orig_std"]
        delay = int((std - parse_time(orig_std_s)).total_seconds() // 60)
        missed = int(item.get("missed_connections", 0))
        if missed < 0: raise ApiError(400, "invalid_connections", "missed_connections 不能为负")
        try:
            if replace:
                conn.execute("""UPDATE assignments SET aircraft_id=?,crew_id=?,new_std=?,new_sta=?,status=?,delay_minutes=?,missed_connections=?
                                WHERE plan_id=? AND flight_id=?""",
                             (item["aircraft_id"], item["crew_id"], iso(std), iso(sta), item.get("status", "planned"), delay, missed, plan_id, item["flight_id"]))
                if conn.execute("SELECT changes()").fetchone()[0] == 0:
                    raise KeyError
            else:
                conn.execute("""INSERT INTO assignments(plan_id,flight_id,aircraft_id,crew_id,orig_std,new_std,new_sta,status,delay_minutes,missed_connections)
                                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                             (plan_id, item["flight_id"], item["aircraft_id"], item["crew_id"], orig_std_s, iso(std), iso(sta), item.get("status", "planned"), delay, missed))
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, "assignment_conflict", "方案中该航班已存在或资源无效") from exc
        except KeyError as exc:
            raise ApiError(404, "assignment_not_found", "待替换的航班调整不存在") from exc

    # ------------------------------------------------------------------
    # 资源租约与中断窗口
    # ------------------------------------------------------------------

    @staticmethod
    def _ttl(body: dict[str, Any] | None = None) -> int:
        ttl = (body or {}).get("lease_ttl_seconds", DEFAULT_LEASE_TTL_SECONDS)
        if not isinstance(ttl, int) or ttl <= 0 or ttl > MAX_LEASE_TTL_SECONDS:
            raise ApiError(400, "invalid_ttl", f"lease_ttl_seconds 必须是 1..{MAX_LEASE_TTL_SECONDS} 的整数")
        return ttl

    @staticmethod
    def _leg_affected(flight: sqlite3.Row, std: datetime, sta: datetime, disruption: sqlite3.Row) -> bool:
        """航段是否落在中断窗口内。飞机/机组故障按航班原始资源判定（换机不等于本航班不受窗口约束）。"""
        if not overlaps(std, sta, parse_time(disruption["starts_at"]), parse_time(disruption["ends_at"])):
            return False
        kind, resource = disruption["kind"], disruption["resource_id"]
        if kind == "airport_closure":
            return flight["origin"] == resource or flight["destination"] == resource
        if kind == "aircraft_fault":
            return flight["aircraft_id"] == resource
        return flight["crew_id"] == resource  # crew_timeout

    def _recompute_to_window(self, conn: sqlite3.Connection, plan_id: int, disruption: sqlite3.Row,
                             force: bool = False) -> list[dict[str, Any]]:
        """按最新中断窗口把方案航段顺延到窗口结束之后，并重算延误（只前推不提前）。
        force=True 时（方案/接管本就绑定该中断）只按时段重叠判定，不依赖航班当前资源。"""
        win_end = parse_time(disruption["ends_at"])
        changed: list[dict[str, Any]] = []
        rows = conn.execute("""SELECT a.*, f.flight_no, f.std orig_std FROM assignments a JOIN flights f ON f.id=a.flight_id
                               WHERE a.plan_id=? AND a.status!='canceled'""", (plan_id,)).fetchall()
        for row in rows:
            std, sta = parse_time(row["new_std"]), parse_time(row["new_sta"])
            duration = sta - std
            flight = conn.execute("SELECT * FROM flights WHERE id=?", (row["flight_id"],)).fetchone()
            baseline = parse_time(row["orig_std"] or flight["std"])
            affected = force or self._leg_affected(flight, std, sta, disruption)
            earliest = max(baseline, win_end) if affected else baseline
            new_std = max(std, earliest)
            new_sta = new_std + duration
            delay = int((new_std - baseline).total_seconds() // 60)
            if new_std != std:
                conn.execute("UPDATE assignments SET new_std=?,new_sta=?,delay_minutes=? WHERE id=?",
                             (iso(new_std), iso(new_sta), delay, row["id"]))
                changed.append({"assignment_id": row["id"], "flight_id": row["flight_id"], "flight_no": row["flight_no"],
                                "old_std": row["new_std"], "new_std": iso(new_std), "delay_minutes": delay})
        return changed

    @staticmethod
    def _held_blocker(conn: sqlite3.Connection, resource_type: str, resource_id: str,
                      start: str, end: str, now: str, exclude_plans: set[int]) -> sqlite3.Row | None:
        sql = """SELECT l.*, p.name plan_name, p.status plan_status FROM resource_leases l
                 JOIN recovery_plans p ON p.id=l.plan_id
                 WHERE l.resource_type=? AND l.resource_id=? AND l.state='held'
                 AND l.starts_at<? AND l.ends_at>? AND (l.expires_at IS NULL OR l.expires_at>?)"""
        params: list[Any] = [resource_type, resource_id, end, start, now]
        if exclude_plans:
            sql += f" AND l.plan_id NOT IN ({','.join('?' for _ in exclude_plans)})"
            params.extend(sorted(exclude_plans))
        return conn.execute(sql, params).fetchone()

    def _free_candidates(self, conn: sqlite3.Connection, resource_type: str, start: str, end: str,
                         now: str, exclude_plans: set[int]) -> list[str]:
        table = "aircraft" if resource_type == "aircraft" else "crew"
        active = [r["id"] for r in conn.execute(f"SELECT id FROM {table} WHERE status='active' ORDER BY id")]
        return [rid for rid in active if not self._held_blocker(conn, resource_type, rid, start, end, now, exclude_plans)]

    def _lease_view(self, conn: sqlite3.Connection, plan_id: int) -> list[dict[str, Any]]:
        now_s = iso()
        items = []
        for row in conn.execute("SELECT * FROM resource_leases WHERE plan_id=? ORDER BY id", (plan_id,)):
            item = dict(row)
            item["valid"] = item["state"] in ("held", "intended") and (item["expires_at"] is None or item["expires_at"] > now_s)
            items.append(item)
        return items

    def _lease_rows(self, conn: sqlite3.Connection, plan_id: int) -> list[dict[str, Any]]:
        return self._lease_view(conn, plan_id)

    def _acquire_leases(self, conn: sqlite3.Connection, plan_id: int, ttl_seconds: int,
                        intended_target_id: int | None = None) -> list[dict[str, Any]]:
        """按当前航段时段（重新）占用飞机和机组。重叠时段只允许一张有效租约；
        冲突时抛出 409，带占用方与空闲候选。intended 租约用于宣告接管意图，不真正占用。"""
        plan = conn.execute("SELECT * FROM recovery_plans WHERE id=?", (plan_id,)).fetchone()
        if not plan:
            raise ApiError(404, "plan_not_found", "方案不存在")
        rows = conn.execute("""SELECT a.*, f.flight_no FROM assignments a JOIN flights f ON f.id=a.flight_id
                               WHERE a.plan_id=? AND a.status!='canceled' ORDER BY a.new_std""", (plan_id,)).fetchall()
        # 同一方案内同资源时段重叠也不允许
        buckets: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in rows:
            for kind in ("aircraft", "crew"):
                buckets.setdefault((kind, row[f"{kind}_id"]), []).append(row)
        for (kind, rid), items in buckets.items():
            for i, left in enumerate(items):
                for right in items[i + 1:]:
                    if overlaps(parse_time(left["new_std"]), parse_time(left["new_sta"]),
                                parse_time(right["new_std"]), parse_time(right["new_sta"])):
                        raise ApiError(400, "plan_leg_overlap", f"方案内 {kind} {rid} 在重叠时段被两个航段占用",
                                       {"resource_type": kind, "resource_id": rid, "assignments": [left["id"], right["id"]]})
        now = utcnow()
        now_s, expires_s = iso(now), iso(now + timedelta(seconds=ttl_seconds))
        exclude = {plan_id}
        if intended_target_id is not None:
            exclude.add(intended_target_id)
        conflicts: list[dict[str, Any]] = []
        for row in rows:
            for kind in ("aircraft", "crew"):
                rid = row[f"{kind}_id"]
                blocker = self._held_blocker(conn, kind, rid, row["new_std"], row["new_sta"], now_s, exclude)
                if blocker:
                    conflicts.append({"assignment_id": row["id"], "flight_id": row["flight_id"], "flight_no": row["flight_no"],
                                      "resource_type": kind, "resource_id": rid,
                                      "window": {"starts_at": row["new_std"], "ends_at": row["new_sta"]},
                                      "occupant": {"plan_id": blocker["plan_id"], "plan_name": blocker["plan_name"],
                                                   "plan_status": blocker["plan_status"], "lease_token": blocker["token"],
                                                   "expires_at": blocker["expires_at"]},
                                      "free_candidates": self._free_candidates(conn, kind, row["new_std"], row["new_sta"], now_s, exclude)})
        if conflicts:
            raise ApiError(409, "lease_conflict", "重叠时段存在有效租约，资源已被其他方案占用", conflicts)
        conn.execute("UPDATE resource_leases SET state='released' WHERE plan_id=? AND state IN ('held','intended')", (plan_id,))
        state = "intended" if intended_target_id is not None else "held"
        for row in rows:
            for kind in ("aircraft", "crew"):
                conn.execute("""INSERT INTO resource_leases(plan_id,assignment_id,resource_type,resource_id,starts_at,ends_at,state,token,expires_at,created_at,renewed_at)
                                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                             (plan_id, row["id"], kind, row[f"{kind}_id"], row["new_std"], row["new_sta"], state,
                              uuid.uuid4().hex, expires_s, now_s, now_s))
        return self._lease_rows(conn, plan_id)

    def _renew_leases(self, conn: sqlite3.Connection, plan_id: int, ttl_seconds: int, states: tuple[str, ...]) -> int:
        """只续仍在有效期内的租约（过期租约视为已丢失，不能续命）。"""
        now_s, expires_s = iso(), iso(utcnow() + timedelta(seconds=ttl_seconds))
        placeholders = ",".join("?" * len(states))
        cur = conn.execute(f"""UPDATE resource_leases SET expires_at=?, renewed_at=?
                               WHERE plan_id=? AND state IN ({placeholders})
                               AND (expires_at IS NULL OR expires_at>?)""",
                           (expires_s, now_s, plan_id, *states, now_s))
        return cur.rowcount

    def update_disruption_window(self, disruption_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"scheduler", "ops_manager"}:
            raise ApiError(403, "window_forbidden", "当前角色不能更新中断窗口")
        expected = body.get("expected_revision")
        if not isinstance(expected, int):
            raise ApiError(400, "revision_required", "expected_revision 必须是整数")
        start, end = parse_time(body.get("starts_at")), parse_time(body.get("ends_at"))
        if end <= start:
            raise ApiError(400, "invalid_times", "中断结束时间必须晚于开始时间")
        with self.repo.tx() as conn:
            disruption = conn.execute("SELECT * FROM disruptions WHERE id=?", (disruption_id,)).fetchone()
            if not disruption:
                raise ApiError(404, "disruption_not_found", "中断事件不存在")
            if disruption["revision"] != expected:
                raise ApiError(409, "revision_conflict", "中断窗口已被其他人更新")
            conn.execute("UPDATE disruptions SET starts_at=?,ends_at=?,revision=revision+1 WHERE id=?",
                         (iso(start), iso(end), disruption_id))
            disruption = conn.execute("SELECT * FROM disruptions WHERE id=?", (disruption_id,)).fetchone()
            invalidated: list[dict[str, Any]] = []
            plans = conn.execute("SELECT * FROM recovery_plans WHERE disruption_id=? AND status IN ('draft','stale')", (disruption_id,)).fetchall()
            for plan in plans:
                # 旧方案立即失效：释放未过期租约，按新窗口重算时刻与延误
                conn.execute("UPDATE resource_leases SET state='released' WHERE plan_id=? AND state IN ('held','intended')", (plan["id"],))
                shifted = self._recompute_to_window(conn, plan["id"], disruption, force=True)
                conn.execute("""UPDATE recovery_plans SET status='stale', revision=revision+1, window_revision=?,
                                metrics_json=NULL, score_json=NULL WHERE id=?""", (disruption["revision"], plan["id"]))
                invalidated.append({"plan_id": plan["id"], "name": plan["name"], "shifted_legs": shifted})
            # 已锁定方案不改动已执行航班，也不追平 window_revision：保留窗口落后标记，供接管决策
            Repository.audit(conn, None, actor, role, "disruption_window_updated",
                             {"disruption_id": disruption_id, "revision": disruption["revision"],
                              "starts_at": iso(start), "ends_at": iso(end), "invalidated_plans": [p["plan_id"] for p in invalidated]})
            return {"disruption": dict(disruption), "invalidated_plans": invalidated}

    def renew_plan_leases(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"scheduler", "ops_manager"}:
            raise ApiError(403, "lease_forbidden", "当前角色不能续约租约")
        ttl = self._ttl(body)
        with self.repo.tx() as conn:
            plan = conn.execute("SELECT * FROM recovery_plans WHERE id=?", (plan_id,)).fetchone()
            if not plan:
                raise ApiError(404, "plan_not_found", "方案不存在")
            if plan["status"] == "stale":
                raise ApiError(409, "plan_stale", "方案已因窗口更新失效，请先重新获取租约")
            count = self._renew_leases(conn, plan_id, ttl, ("held", "intended"))
            if count == 0:
                raise ApiError(409, "lease_lost", "方案没有有效租约，请重新获取",
                               {"reacquire": f"/api/plans/{plan_id}/leases"})
            Repository.audit(conn, plan_id, actor, role, "leases_renewed", {"ttl_seconds": ttl, "count": count})
            return {"renewed": count, "leases": self._lease_rows(conn, plan_id)}

    def reacquire_plan_leases(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"scheduler", "ops_manager"}:
            raise ApiError(403, "lease_forbidden", "当前角色不能获取租约")
        ttl = self._ttl(body)
        with self.repo.tx() as conn:
            plan = conn.execute("SELECT * FROM recovery_plans WHERE id=?", (plan_id,)).fetchone()
            if not plan:
                raise ApiError(404, "plan_not_found", "方案不存在")
            if plan["status"] in ("locked", "superseded"):
                raise ApiError(409, "plan_not_draft", f"{plan['status']} 方案不能再获取租约")
            target_id = plan["takeover_target_id"]
            leases = self._acquire_leases(conn, plan_id, ttl, intended_target_id=target_id)
            # 窗口更新后重新拿到租约，视为调度员确认按重算结果继续推进
            if plan["status"] == "stale":
                conn.execute("UPDATE recovery_plans SET status='draft', revision=revision+1 WHERE id=?", (plan_id,))
            Repository.audit(conn, plan_id, actor, role, "leases_acquired",
                             {"ttl_seconds": ttl, "count": len(leases), "takeover_target_id": target_id,
                              "reactivated_from_stale": plan["status"] == "stale"})
            return {"plan": self.get_plan(plan_id), "leases": leases}

    def release_plan_leases(self, plan_id: int, actor: str, role: str) -> dict[str, Any]:
        if role not in {"scheduler", "ops_manager"}:
            raise ApiError(403, "lease_forbidden", "当前角色不能释放租约")
        with self.repo.tx() as conn:
            plan = conn.execute("SELECT * FROM recovery_plans WHERE id=?", (plan_id,)).fetchone()
            if not plan:
                raise ApiError(404, "plan_not_found", "方案不存在")
            cur = conn.execute("UPDATE resource_leases SET state='released' WHERE plan_id=? AND state IN ('held','intended')", (plan_id,))
            Repository.audit(conn, plan_id, actor, role, "leases_released", {"count": cur.rowcount})
            return {"released": cur.rowcount}

    def add_assignment(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"scheduler", "ops_manager"}: raise ApiError(403, "assignment_forbidden", "当前角色不能修改方案")
        expected = body.get("expected_revision")
        if not isinstance(expected, int): raise ApiError(400, "revision_required", "expected_revision 必须是整数")
        ttl = self._ttl(body)
        with self.repo.tx() as conn:
            plan = conn.execute("SELECT * FROM recovery_plans WHERE id=?", (plan_id,)).fetchone()
            if not plan: raise ApiError(404, "plan_not_found", "方案不存在")
            if plan["status"] != "draft":
                if plan["status"] == "stale": raise ApiError(409, "plan_stale", "方案已因窗口更新失效，请重新获取租约并审阅")
                raise ApiError(409, "plan_locked", "已锁定方案不能修改")
            if plan["revision"] != expected: raise ApiError(409, "revision_conflict", "方案已被其他人更新")
            self._insert_assignment(conn, plan_id, body, replace=True)
            conn.execute("UPDATE recovery_plans SET revision=revision+1 WHERE id=?", (plan_id,))
            target_id = plan["takeover_target_id"]
            leases = self._acquire_leases(conn, plan_id, ttl, intended_target_id=target_id)
            Repository.audit(conn, plan_id, actor, role, "assignment_reassigned",
                             {"flight_id": body.get("flight_id"), "leases": len(leases), "takeover_target_id": target_id})
            result = self.get_plan(plan_id)
            result["leases"] = leases
            return result

    def _validate_plan(self, conn: sqlite3.Connection, plan_id: int) -> list[dict[str, Any]]:
        plan = conn.execute("SELECT * FROM recovery_plans WHERE id=?", (plan_id,)).fetchone()
        if not plan: raise ApiError(404, "plan_not_found", "方案不存在")
        rows = [dict(r) for r in conn.execute("""SELECT a.*, f.flight_no, f.origin, f.destination, f.passenger_count, f.status flight_status
                                                  FROM assignments a JOIN flights f ON f.id=a.flight_id WHERE a.plan_id=? ORDER BY a.new_std""", (plan_id,))]
        if not rows: raise ApiError(409, "empty_plan", "方案没有飞行调整")
        problems: list[dict[str, Any]] = []
        by_aircraft: dict[str, list[dict[str, Any]]] = {}
        by_crew: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row["status"] == "canceled": continue
            std, sta = parse_time(row["new_std"]), parse_time(row["new_sta"])
            aircraft = conn.execute("SELECT * FROM aircraft WHERE id=?", (row["aircraft_id"],)).fetchone()
            crew = conn.execute("SELECT * FROM crew WHERE id=?", (row["crew_id"],)).fetchone()
            origin = conn.execute("SELECT * FROM airports WHERE code=?", (row["origin"],)).fetchone()
            destination = conn.execute("SELECT * FROM airports WHERE code=?", (row["destination"],)).fetchone()
            if not aircraft or aircraft["status"] != "active": problems.append({"assignment_id": row["id"], "code": "aircraft_unavailable"})
            elif parse_time(aircraft["maintenance_due"]) < sta: problems.append({"assignment_id": row["id"], "code": "maintenance_due", "resource": aircraft["id"]})
            if not crew or crew["status"] != "active": problems.append({"assignment_id": row["id"], "code": "crew_unavailable"})
            if not origin or not destination: problems.append({"assignment_id": row["id"], "code": "airport_unknown"})
            if crew:
                duty_start, max_duty = parse_time(crew["duty_start"]), crew["max_duty_minutes"]
                if (sta - duty_start).total_seconds() / 60 > max_duty: problems.append({"assignment_id": row["id"], "code": "duty_limit", "resource": crew["id"]})
            if destination:
                curfew_start, curfew_end = parse_clock(destination["curfew_start"]), parse_clock(destination["curfew_end"])
                permit = conn.execute("""SELECT * FROM permits WHERE origin=? AND destination=? AND valid_from<=? AND valid_to>=?""",
                                      (row["origin"], row["destination"], row["new_sta"], row["new_sta"])).fetchone()
                arrival_clock = sta.timetz().replace(tzinfo=None)
                inside = arrival_clock >= curfew_start or arrival_clock < curfew_end if curfew_start > curfew_end else curfew_start <= arrival_clock < curfew_end
                if inside and not (permit and permit["curfew_exempt"]): problems.append({"assignment_id": row["id"], "code": "airport_curfew"})
                if row["origin"] != row["destination"] and not permit: problems.append({"assignment_id": row["id"], "code": "route_permit_missing"})
                elif row["origin"] != row["destination"] and not (parse_time(permit["valid_from"]) <= std <= parse_time(permit["valid_to"])):
                    problems.append({"assignment_id": row["id"], "code": "route_permit_window"})
            by_aircraft.setdefault(row["aircraft_id"], []).append(row)
            by_crew.setdefault(row["crew_id"], []).append(row)
        for bucket_name, buckets in (("aircraft", by_aircraft), ("crew", by_crew)):
            for resource, items in buckets.items():
                for i, left in enumerate(items):
                    for right in items[i + 1:]:
                        if overlaps(parse_time(left["new_std"]), parse_time(left["new_sta"]), parse_time(right["new_std"]), parse_time(right["new_sta"])):
                            problems.append({"code": f"{bucket_name}_overlap", "resource": resource, "assignments": [left["id"], right["id"]]})
        return problems

    def validate_plan(self, plan_id: int, actor: str, role: str) -> dict[str, Any]:
        if role not in {"scheduler", "ops_manager", "auditor"}: raise ApiError(403, "validate_forbidden", "当前角色不能校验方案")
        with self.repo.tx() as conn:
            problems = self._validate_plan(conn, plan_id)
            if not problems:
                metrics = self._metrics(conn, plan_id)
                conn.execute("UPDATE recovery_plans SET metrics_json=?,score_json=? WHERE id=?", (json.dumps(metrics, ensure_ascii=False), json.dumps(self._score(metrics)), plan_id))
            return {"valid": not problems, "problems": problems, "plan": self.get_plan(plan_id)}

    def _metrics(self, conn: sqlite3.Connection, plan_id: int) -> dict[str, Any]:
        rows = conn.execute("""SELECT a.*,f.passenger_count FROM assignments a JOIN flights f ON f.id=a.flight_id WHERE a.plan_id=?""", (plan_id,)).fetchall()
        canceled = sum(1 for row in rows if row["status"] == "canceled")
        return {"flight_count": len(rows), "canceled": canceled, "total_delay_minutes": sum(max(0, row["delay_minutes"]) for row in rows),
                "affected_passengers": sum(row["passenger_count"] for row in rows), "missed_connections": sum(row["missed_connections"] for row in rows)}

    @staticmethod
    def _score(metrics: dict[str, Any]) -> dict[str, int]:
        score = metrics["canceled"] * 100000 + metrics["missed_connections"] * 5000 + metrics["total_delay_minutes"] * 100 + metrics["affected_passengers"]
        return {"cost_score": score, "lower_is_better": 1}

    def lock_plan(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "ops_manager": raise ApiError(403, "lock_forbidden", "只有运行经理可以锁定恢复方案")
        expected = body.get("expected_revision")
        if not isinstance(expected, int): raise ApiError(400, "revision_required", "expected_revision 必须是整数")
        with self.repo.tx() as conn:
            plan = conn.execute("SELECT * FROM recovery_plans WHERE id=?", (plan_id,)).fetchone()
            if not plan: raise ApiError(404, "plan_not_found", "方案不存在")
            if plan["status"] == "locked": return self.get_plan(plan_id)
            if plan["status"] == "stale": raise ApiError(409, "plan_stale", "方案已因窗口更新失效，请重新获取租约并审阅")
            if plan["status"] == "superseded": raise ApiError(409, "plan_superseded", "方案已被接管方案取代")
            if plan["revision"] != expected: raise ApiError(409, "revision_conflict", "方案版本已变化")
            if plan["takeover_target_id"] is not None:
                raise ApiError(409, "takeover_required", "接管方案必须通过 takeover 接口接手，不能直接锁定")
            problems = self._validate_plan(conn, plan_id)
            if problems: raise ApiError(409, "plan_invalid", "方案未通过约束校验", problems)
            # 每个航段的飞机与机组都必须持有当前有效（未过期、未释放、未被接管）的租约
            lease_errors: list[dict[str, Any]] = []
            now_s = iso()
            for row in conn.execute("SELECT * FROM assignments WHERE plan_id=? AND status!='canceled'", (plan_id,)).fetchall():
                for kind in ("aircraft", "crew"):
                    rid = row[f"{kind}_id"]
                    own = conn.execute("""SELECT * FROM resource_leases WHERE assignment_id=? AND resource_type=? AND resource_id=?
                                          AND state='held' AND (expires_at IS NULL OR expires_at>?)""",
                                       (row["id"], kind, rid, now_s)).fetchone()
                    if not own:
                        blocker = self._held_blocker(conn, kind, rid, row["new_std"], row["new_sta"], now_s, {plan_id})
                        lease_errors.append({"assignment_id": row["id"], "resource_type": kind, "resource_id": rid,
                                             "code": "lease_lost",
                                             "occupant": ({"plan_id": blocker["plan_id"], "plan_name": blocker["plan_name"],
                                                           "plan_status": blocker["plan_status"], "expires_at": blocker["expires_at"]}
                                                          if blocker else None),
                                             "free_candidates": self._free_candidates(conn, kind, row["new_std"], row["new_sta"], now_s, {plan_id})})
            if lease_errors:
                raise ApiError(409, "locked_resource_conflict", "有效租约缺失或已被占用，请重新续约或调整资源", lease_errors)
            metrics = self._metrics(conn, plan_id)
            disruption = conn.execute("SELECT revision FROM disruptions WHERE id=?", (plan["disruption_id"],)).fetchone()
            conn.execute("""UPDATE recovery_plans SET status='locked',metrics_json=?,score_json=?,locked_at=?,locked_by=?,window_revision=? WHERE id=?""",
                         (json.dumps(metrics, ensure_ascii=False), json.dumps(self._score(metrics)), iso(), actor, disruption["revision"], plan_id))
            # 锁定方案的租约转为永久有效（与锁定方案同寿命）
            conn.execute("UPDATE resource_leases SET expires_at=NULL WHERE plan_id=? AND state='held'", (plan_id,))
            for row in conn.execute("""SELECT a.*,f.flight_no FROM assignments a JOIN flights f ON f.id=a.flight_id WHERE a.plan_id=? AND a.status!='canceled'""", (plan_id,)):
                conn.execute("UPDATE flights SET std=?,sta=?,aircraft_id=?,crew_id=?,delay_minutes=?,revision=revision+1,updated_at=? WHERE id=?",
                             (row["new_std"], row["new_sta"], row["aircraft_id"], row["crew_id"], max(0, row["delay_minutes"]), iso(), row["flight_id"]))
                conn.execute("UPDATE assignments SET status='active' WHERE id=?", (row["id"],))
            Repository.audit(conn, plan_id, actor, role, "plan_locked", {"metrics": metrics})
            return self.get_plan(plan_id)

    def _takeover_once(self, conn: sqlite3.Connection, plan_id: int, target_id: int, actor: str, role: str) -> dict[str, Any]:
        """一次接管事务：按最新窗口重算 -> 校验 -> 移交租约 -> 写入航班。失败抛异常即随事务回滚到原租约。"""
        plan = conn.execute("SELECT * FROM recovery_plans WHERE id=?", (plan_id,)).fetchone()
        if not plan:
            raise ApiError(404, "plan_not_found", "接管方案不存在")
        target = conn.execute("SELECT * FROM recovery_plans WHERE id=?", (target_id,)).fetchone()
        if not target:
            raise ApiError(404, "target_plan_not_found", "被接管方案不存在")
        if plan["status"] != "draft":
            raise ApiError(409, "plan_not_draft", f"{plan['status']} 方案不能执行接管")
        if target["status"] != "locked":
            raise ApiError(409, "target_not_locked", "被接管方案不是已锁定状态")
        if plan["disruption_id"] != target["disruption_id"]:
            raise ApiError(409, "target_mismatch", "两个方案必须针对同一中断事件")
        if target_id == plan_id:
            raise ApiError(400, "invalid_target", "不能接管自己")
        disruption = conn.execute("SELECT * FROM disruptions WHERE id=?", (plan["disruption_id"],)).fetchone()

        # 1) 按最新窗口重算接管方案的时刻与延误
        before = {r["flight_id"]: {"new_std": r["new_std"], "new_sta": r["new_sta"], "delay_minutes": r["delay_minutes"]}
                  for r in conn.execute("SELECT * FROM assignments WHERE plan_id=? AND status!='canceled'", (plan_id,))}
        shifted = self._recompute_to_window(conn, plan_id, disruption, force=True)

        # 2) 约束校验（维护、执勤、宵禁、许可、方案内重叠）
        problems = self._validate_plan(conn, plan_id)
        if problems:
            raise ApiError(409, "plan_invalid", "接管方案按最新窗口重算后未通过校验", problems)

        # 3) 租约核对：除被接管方案外不得持有重叠 held 租约；自己的 intended 要覆盖每条航段
        now_s = iso()
        lease_errors: list[dict[str, Any]] = []
        for row in conn.execute("SELECT * FROM assignments WHERE plan_id=? AND status!='canceled'", (plan_id,)).fetchall():
            for kind in ("aircraft", "crew"):
                rid = row[f"{kind}_id"]
                blocker = self._held_blocker(conn, kind, rid, row["new_std"], row["new_sta"], now_s, {plan_id, target_id})
                if blocker:
                    lease_errors.append({"assignment_id": row["id"], "resource_type": kind, "resource_id": rid,
                                         "code": "lease_conflict",
                                         "occupant": {"plan_id": blocker["plan_id"], "plan_name": blocker["plan_name"],
                                                      "plan_status": blocker["plan_status"]},
                                         "free_candidates": self._free_candidates(conn, kind, row["new_std"], row["new_sta"], now_s, {plan_id, target_id})})
        if lease_errors:
            raise ApiError(409, "lease_conflict", "接管资源被其他方案占用，被接管目标之外存在有效租约", lease_errors)

        # 4) 移交租约：目标的永久租约 -> superseded；本方案 intended -> held 并对齐到当前航段
        conn.execute("UPDATE resource_leases SET state='superseded' WHERE plan_id=? AND state='held'", (target_id,))
        conn.execute("UPDATE resource_leases SET state='released' WHERE plan_id=? AND state IN ('intended','held')", (plan_id,))
        for row in conn.execute("SELECT * FROM assignments WHERE plan_id=? AND status!='canceled'", (plan_id,)):
            for kind in ("aircraft", "crew"):
                conn.execute("""INSERT INTO resource_leases(plan_id,assignment_id,resource_type,resource_id,starts_at,ends_at,state,token,expires_at,created_at,renewed_at)
                                VALUES(?,?,?,?,?,?,'held',?,NULL,?,?)""",
                             (plan_id, row["id"], kind, row[f"{kind}_id"], row["new_std"], row["new_sta"],
                              uuid.uuid4().hex, now_s, now_s))

        # 5) 写入航班与方案状态
        metrics = self._metrics(conn, plan_id)
        old_metrics = json.loads(target["metrics_json"]) if target["metrics_json"] else {}
        conn.execute("""UPDATE recovery_plans SET status='locked', metrics_json=?, score_json=?, locked_at=?, locked_by=?,
                        revision=revision+1, window_revision=?, takeover_target_id=? WHERE id=?""",
                     (json.dumps(metrics, ensure_ascii=False), json.dumps(self._score(metrics)), iso(), actor,
                      disruption["revision"], target_id, plan_id))
        conn.execute("UPDATE recovery_plans SET status='superseded' WHERE id=?", (target_id,))
        conn.execute("UPDATE assignments SET status='superseded' WHERE plan_id=?", (target_id,))
        for row in conn.execute("SELECT * FROM assignments WHERE plan_id=? AND status!='canceled'", (plan_id,)):
            conn.execute("UPDATE flights SET std=?,sta=?,aircraft_id=?,crew_id=?,delay_minutes=?,revision=revision+1,updated_at=? WHERE id=?",
                         (row["new_std"], row["new_sta"], row["aircraft_id"], row["crew_id"], max(0, row["delay_minutes"]), iso(), row["flight_id"]))
            conn.execute("UPDATE assignments SET status='active' WHERE id=?", (row["id"],))

        # 6) 接管差异（控制台留存）
        after_rows = conn.execute("""SELECT a.*, f.flight_no FROM assignments a JOIN flights f ON f.id=a.flight_id
                                     WHERE a.plan_id=? AND a.status='active' ORDER BY a.new_std""", (plan_id,)).fetchall()
        old_rows = {r["flight_id"]: dict(r) for r in conn.execute(
            "SELECT * FROM assignments WHERE plan_id=?", (target_id,))}
        legs: list[dict[str, Any]] = []
        for row in after_rows:
            prev = before.get(row["flight_id"], {})
            old = old_rows.get(row["flight_id"], {})
            legs.append({"flight_id": row["flight_id"], "flight_no": row["flight_no"],
                         "aircraft_from": old.get("aircraft_id"), "aircraft_to": row["aircraft_id"],
                         "crew_from": old.get("crew_id"), "crew_to": row["crew_id"],
                         "target_std": old.get("new_std"), "draft_std": prev.get("new_std"), "new_std": row["new_std"],
                         "delay_from_window_recompute_minutes": max(0, row["delay_minutes"] - prev.get("delay_minutes", row["delay_minutes"])),
                         "target_delay_minutes": old.get("delay_minutes", 0), "new_delay_minutes": row["delay_minutes"]})
        diff = {"disruption_id": plan["disruption_id"], "window_revision": disruption["revision"],
                "window": {"starts_at": disruption["starts_at"], "ends_at": disruption["ends_at"]},
                "target_plan_id": target_id, "taking_plan_id": plan_id,
                "metrics_target": old_metrics, "metrics_new": metrics,
                "total_delay_delta_minutes": metrics["total_delay_minutes"] - old_metrics.get("total_delay_minutes", 0),
                "legs": legs, "shifted_by_window": shifted}
        conn.execute("""INSERT INTO takeovers(disruption_id,taking_plan_id,target_plan_id,window_revision,attempts,diff_json,created_by,created_at)
                        VALUES(?,?,?,?,?,?,?,?)""",
                     (plan["disruption_id"], plan_id, target_id, disruption["revision"], -1,
                      json.dumps(diff, ensure_ascii=False, sort_keys=True), actor, iso()))
        takeover_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        Repository.audit(conn, plan_id, actor, role, "plan_takeover",
                         {"takeover_id": takeover_id, "target_plan_id": target_id, "window_revision": disruption["revision"],
                          "total_delay_delta_minutes": diff["total_delay_delta_minutes"]})
        return {"takeover_id": takeover_id, "diff": diff, "plan": self.get_plan(plan_id)}

    def takeover_plan(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        """运行经理让接管方案接手已锁定方案。写入失败（提交失败）时回滚到原租约并重试。"""
        if role != "ops_manager":
            raise ApiError(403, "takeover_forbidden", "只有运行经理可以执行接管")
        target_id = body.get("target_plan_id")
        if not isinstance(target_id, int):
            raise ApiError(400, "target_required", "target_plan_id 必须是整数")
        last_error: Exception | None = None
        for attempt in range(1, TAKEOVER_WRITE_ATTEMPTS + 1):
            try:
                with self.repo.tx() as conn:
                    result = self._takeover_once(conn, plan_id, target_id, actor, role)
                    conn.execute("UPDATE takeovers SET attempts=? WHERE id=?", (attempt, result["takeover_id"]))
                    result["attempts"] = attempt
                    return result
            except ApiError:
                raise  # 业务校验失败不重试
            except sqlite3.Error as exc:  # 提交/写入失败：事务已回滚到原租约，稍后重试
                last_error = exc
                if attempt < TAKEOVER_WRITE_ATTEMPTS:
                    sleep_seconds(TAKEOVER_RETRY_BACKOFF * attempt)
        raise ApiError(503, "takeover_write_failed", "接管写入连续失败，已保持原租约不变",
                       {"attempts": TAKEOVER_WRITE_ATTEMPTS, "last_error": str(last_error)})

    def list_leases(self) -> dict[str, Any]:
        conn = self.repo.conn
        now_s = iso()
        items = []
        for row in conn.execute("""SELECT l.*, p.name plan_name, p.status plan_status FROM resource_leases l
                                   JOIN recovery_plans p ON p.id=l.plan_id ORDER BY l.resource_type,l.resource_id,l.starts_at"""):
            item = dict(row)
            item["valid"] = item["state"] in ("held", "intended") and (item["expires_at"] is None or item["expires_at"] > now_s)
            items.append(item)
        return {"server_time": now_s, "leases": items}

    def list_takeovers(self, disruption_id: int | None = None) -> dict[str, Any]:
        conn = self.repo.conn
        if disruption_id is None:
            rows = conn.execute("SELECT * FROM takeovers ORDER BY id DESC LIMIT 50").fetchall()
        else:
            rows = conn.execute("SELECT * FROM takeovers WHERE disruption_id=? ORDER BY id DESC", (disruption_id,)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["diff"] = json.loads(row["diff_json"])
            del item["diff_json"]
            items.append(item)
        return {"takeovers": items}

    def cancel_flight(self, flight_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"scheduler", "ops_manager"}: raise ApiError(403, "cancel_forbidden", "当前角色不能取消航班")
        reason = str(body.get("reason", "")).strip()
        if not reason: raise ApiError(400, "reason_required", "取消原因必填")
        with self.repo.tx() as conn:
            flight = conn.execute("SELECT * FROM flights WHERE id=?", (flight_id,)).fetchone()
            if not flight: raise ApiError(404, "flight_not_found", "航班不存在")
            if flight["status"] == "canceled": return {"flight": dict(flight), "idempotent": True}
            conn.execute("UPDATE flights SET status='canceled',cancel_reason=?,revision=revision+1,updated_at=? WHERE id=?", (reason, iso(), flight_id))
            Repository.audit(conn, None, actor, role, "flight_canceled", {"flight_id": flight_id, "reason": reason})
            return {"flight": dict(conn.execute("SELECT * FROM flights WHERE id=?", (flight_id,)).fetchone()), "idempotent": False}

    def recover_flight(self, flight_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"scheduler", "ops_manager"}: raise ApiError(403, "recover_forbidden", "当前角色不能恢复航班")
        expected = body.get("expected_revision")
        if not isinstance(expected, int): raise ApiError(400, "revision_required", "expected_revision 必须是整数")
        with self.repo.tx() as conn:
            flight = conn.execute("SELECT * FROM flights WHERE id=?", (flight_id,)).fetchone()
            if not flight: raise ApiError(404, "flight_not_found", "航班不存在")
            if flight["revision"] != expected: raise ApiError(409, "revision_conflict", "航班版本已变化")
            if flight["status"] != "canceled": raise ApiError(409, "not_canceled", "只有取消航班可以恢复")
            std, sta = parse_time(body.get("new_std")), parse_time(body.get("new_sta"))
            if sta <= std: raise ApiError(400, "invalid_times", "到达时间必须晚于起飞时间")
            aircraft_id, crew_id = body.get("aircraft_id", flight["aircraft_id"]), body.get("crew_id", flight["crew_id"])
            conn.execute("""UPDATE flights SET status='scheduled',std=?,sta=?,aircraft_id=?,crew_id=?,cancel_reason=NULL,
                            delay_minutes=0,revision=revision+1,updated_at=? WHERE id=?""",
                         (iso(std), iso(sta), aircraft_id, crew_id, iso(), flight_id))
            Repository.audit(conn, None, actor, role, "flight_recovered", {"flight_id": flight_id})
            return {"flight": dict(conn.execute("SELECT * FROM flights WHERE id=?", (flight_id,)).fetchone())}

    def get_plan(self, plan_id: int) -> dict[str, Any]:
        conn = self.repo.conn
        plan = conn.execute("SELECT * FROM recovery_plans WHERE id=?", (plan_id,)).fetchone()
        if not plan: raise ApiError(404, "plan_not_found", "方案不存在")
        assignments = [dict(r) for r in conn.execute("""SELECT a.*,f.flight_no,f.origin,f.destination,f.passenger_count FROM assignments a JOIN flights f ON f.id=a.flight_id WHERE a.plan_id=? ORDER BY a.new_std""", (plan_id,))]
        result = dict(plan)
        result["metrics"] = json.loads(plan["metrics_json"]) if plan["metrics_json"] else self._metrics(conn, plan_id)
        result["score"] = json.loads(plan["score_json"]) if plan["score_json"] else None
        result["assignments"] = assignments
        now_s = iso()
        leases = []
        for row in conn.execute("SELECT * FROM resource_leases WHERE plan_id=? ORDER BY id", (plan_id,)):
            item = dict(row)
            item["valid"] = item["state"] in ("held", "intended") and (item["expires_at"] is None or item["expires_at"] > now_s)
            leases.append(item)
        result["leases"] = leases
        disruption = conn.execute("SELECT revision FROM disruptions WHERE id=?", (plan["disruption_id"],)).fetchone()
        result["window_stale"] = bool(disruption and disruption["revision"] != plan["window_revision"])
        if plan["takeover_target_id"] is not None:
            target = conn.execute("SELECT id,name,status FROM recovery_plans WHERE id=?", (plan["takeover_target_id"],)).fetchone()
            result["takeover_target"] = dict(target) if target else None
        return result

    def compare_plans(self, disruption_id: int) -> dict[str, Any]:
        plans = []
        for row in self.repo.conn.execute("SELECT id FROM recovery_plans WHERE disruption_id=? ORDER BY id", (disruption_id,)):
            plan = self.get_plan(row["id"])
            if not plan["score"]:
                problems = self._validate_plan(self.repo.conn, row["id"])
                plan["valid"] = not problems
            else:
                plan["valid"] = True
            plans.append(plan)
        plans.sort(key=lambda item: item["score"]["cost_score"] if item["score"] else 10**18)
        return {"disruption_id": disruption_id, "recommended_plan_id": plans[0]["id"] if plans else None, "plans": plans}

    def state(self) -> dict[str, Any]:
        conn = self.repo.conn
        flights = [dict(r) for r in conn.execute("SELECT * FROM flights ORDER BY std")]
        plans = [self.get_plan(r["id"]) for r in conn.execute("SELECT id FROM recovery_plans ORDER BY id DESC LIMIT 20")]
        now_s = iso()
        leases = []
        for row in conn.execute("""SELECT l.*, p.name plan_name FROM resource_leases l JOIN recovery_plans p ON p.id=l.plan_id
                                   WHERE l.state IN ('held','intended') AND (l.expires_at IS NULL OR l.expires_at>?) ORDER BY l.starts_at""", (now_s,)):
            leases.append(dict(row))
        takeovers = []
        for row in conn.execute("SELECT * FROM takeovers ORDER BY id DESC LIMIT 20"):
            item = dict(row); item["diff"] = json.loads(row["diff_json"]); del item["diff_json"]
            takeovers.append(item)
        return {"flights": flights, "disruptions": [dict(r) for r in conn.execute("SELECT * FROM disruptions ORDER BY id DESC")],
                "plans": plans, "active_leases": leases, "takeovers": takeovers, "server_time": iso()}


def respond(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    raw = json.dumps(payload, ensure_ascii=False, default=str).encode()
    handler.send_response(status); handler.send_header("Content-Type", "application/json; charset=utf-8"); handler.send_header("Content-Length", str(len(raw))); handler.end_headers(); handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    service: AirlineRecoveryService
    web_root: Path
    def log_message(self, fmt: str, *args: Any) -> None: print(f"{self.address_string()} - {fmt % args}")
    def body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length: return {}
        try: value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc: raise ApiError(400, "invalid_json", "请求体不是有效 JSON") from exc
        if not isinstance(value, dict): raise ApiError(400, "invalid_json", "请求体必须是对象")
        return value
    def get_api(self, path: str) -> tuple[int, Any]:
        if path == "/health": return 200, {"status": "ok", "service": "airline-recovery"}
        actor, role = self.service.identity(self.headers)
        if path == "/api/state": return 200, self.service.state()
        if path == "/api/leases": return 200, self.service.list_leases()
        if path == "/api/takeovers": return 200, self.service.list_takeovers()
        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[:2] == ["api", "plans"] and parts[2].isdigit(): return 200, self.service.get_plan(int(parts[2]))
        if len(parts) == 4 and parts[:2] == ["api", "disruptions"] and parts[2].isdigit() and parts[3] == "compare": return 200, self.service.compare_plans(int(parts[2]))
        if len(parts) == 4 and parts[:2] == ["api", "disruptions"] and parts[2].isdigit() and parts[3] == "takeovers":
            return 200, self.service.list_takeovers(int(parts[2]))
        raise ApiError(404, "not_found", "接口不存在")
    def post_api(self, path: str) -> tuple[int, Any]:
        actor, role = self.service.identity(self.headers); body = self.body(); parts = [p for p in path.split("/") if p]
        table = {
            "/api/airports": lambda: (201, self.service.seed_airport(actor, role, body)),
            "/api/aircraft": lambda: (201, self.service.seed_aircraft(actor, role, body)),
            "/api/crew": lambda: (201, self.service.seed_crew(actor, role, body)),
            "/api/permits": lambda: (201, self.service.create_permit(actor, role, body)),
            "/api/flights": lambda: (201, self.service.create_flight(actor, role, body)),
            "/api/disruptions": lambda: (201, self.service.create_disruption(actor, role, body)),
            "/api/recovery-plans": lambda: (201, self.service.create_plan(actor, role, body)),
        }
        if path in table: return table[path]()
        if len(parts) == 4 and parts[:2] == ["api", "disruptions"] and parts[2].isdigit() and parts[3] == "window":
            return 200, self.service.update_disruption_window(int(parts[2]), actor, role, body)
        if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[2].isdigit():
            plan_id, action = int(parts[2]), parts[3]
            if action == "assignments": return 200, self.service.add_assignment(plan_id, actor, role, body)
            if action == "validate": return 200, self.service.validate_plan(plan_id, actor, role)
            if action == "lock": return 200, self.service.lock_plan(plan_id, actor, role, body)
            if action == "leases": return 200, self.service.reacquire_plan_leases(plan_id, actor, role, body)
            if action == "takeover": return 200, self.service.takeover_plan(plan_id, actor, role, body)
        if len(parts) == 5 and parts[:2] == ["api", "plans"] and parts[2].isdigit() and parts[3] == "leases":
            plan_id, action = int(parts[2]), parts[4]
            if action == "renew": return 200, self.service.renew_plan_leases(plan_id, actor, role, body)
            if action == "release": return 200, self.service.release_plan_leases(plan_id, actor, role)
        if len(parts) == 4 and parts[:2] == ["api", "flights"] and parts[2].isdigit():
            flight_id, action = int(parts[2]), parts[3]
            if action == "cancel": return 200, self.service.cancel_flight(flight_id, actor, role, body)
            if action == "recover": return 200, self.service.recover_flight(flight_id, actor, role, body)
        raise ApiError(404, "not_found", "接口不存在")
    def handle_request(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            if method == "GET" and parsed.path == "/":
                raw = (self.web_root / "index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
            status, payload = self.get_api(parsed.path) if method == "GET" else self.post_api(parsed.path)
            respond(self, status, payload)
        except ApiError as exc:
            payload = {"error": exc.code, "message": exc.message}
            if exc.details is not None: payload["details"] = exc.details
            respond(self, exc.status, payload)
        except Exception as exc:
            print(f"unhandled error: {exc!r}"); respond(self, 500, {"error": "internal_error", "message": str(exc)})
    def do_GET(self) -> None: self.handle_request("GET")
    def do_POST(self) -> None: self.handle_request("POST")


def create_server(db_path: str | Path, host: str = "127.0.0.1", port: int = PORT) -> ThreadingHTTPServer:
    service = AirlineRecoveryService(db_path)
    handler = type("AirlineHandler", (Handler,), {"service": service, "web_root": Path(__file__).resolve().parent / "static"})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=PORT); parser.add_argument("--db", default=os.environ.get("AIRLINE_DB", "airline_recovery.db")); args = parser.parse_args()
    server = create_server(args.db, args.host, args.port); print(f"airline-recovery listening on http://{args.host}:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__ == "__main__": main()
