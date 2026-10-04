import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import AirlineRecoveryService, ApiError, iso, utcnow
from datetime import datetime, timezone


class ScenarioCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = AirlineRecoveryService(Path(self.tmp.name) / "test.db")
        base = utcnow() + timedelta(days=1)
        self.svc.seed_airport("ops", "ops_manager", {"code": "AAA", "country": "CN", "curfew_start": "23:00", "curfew_end": "05:00"})
        self.svc.seed_airport("ops", "ops_manager", {"code": "BBB", "country": "CN", "curfew_start": "23:00", "curfew_end": "05:00"})
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC1", "model": "A320", "maintenance_due": iso(base + timedelta(days=5))})
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC2", "model": "A320", "maintenance_due": iso(base + timedelta(days=5))})
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR1", "name": "甲组", "base": "AAA", "duty_start": iso(base - timedelta(hours=2)), "max_duty_minutes": 720})
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR2", "name": "乙组", "base": "AAA", "duty_start": iso(base - timedelta(hours=2)), "max_duty_minutes": 720})
        self.svc.create_permit("ops", "ops_manager", {"origin": "AAA", "destination": "BBB", "valid_from": iso(base - timedelta(days=1)), "valid_to": iso(base + timedelta(days=2))})
        self.base = base

    def tearDown(self): self.tmp.cleanup()

    def make_flight(self, number, aircraft, crew):
        return self.svc.create_flight("sched", "scheduler", {"flight_no": number, "origin": "AAA", "destination": "BBB", "std": iso(self.base), "sta": iso(self.base + timedelta(hours=2)), "aircraft_id": aircraft, "crew_id": crew, "passenger_count": 150})

class AirlineFlowTest(ScenarioCase):
    def test_complete_recovery_and_manual_recovery(self):
        flight = self.make_flight("AB100", "AC1", "CR1")
        disruption = self.svc.create_disruption("sched", "scheduler", {"kind": "aircraft_fault", "resource_id": "AC1", "starts_at": iso(self.base - timedelta(hours=1)), "ends_at": iso(self.base + timedelta(hours=3))})
        plan = self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": "换飞机并延误", "assignments": [{"flight_id": flight["id"], "aircraft_id": "AC2", "crew_id": "CR2", "new_std": iso(self.base + timedelta(hours=3)), "new_sta": iso(self.base + timedelta(hours=5)), "missed_connections": 4}]})
        check = self.svc.validate_plan(plan["id"], "auditor", "auditor")
        self.assertTrue(check["valid"], check["problems"])
        locked = self.svc.lock_plan(plan["id"], "ops", "ops_manager", {"expected_revision": 1})
        self.assertEqual(locked["status"], "locked")
        self.assertEqual(locked["metrics"]["affected_passengers"], 150)
        canceled = self.svc.cancel_flight(flight["id"], "sched", "scheduler", {"reason": "后续机务检查"})["flight"]
        recovered = self.svc.recover_flight(flight["id"], "sched", "scheduler", {"expected_revision": canceled["revision"], "new_std": iso(self.base + timedelta(hours=8)), "new_sta": iso(self.base + timedelta(hours=10))})["flight"]
        self.assertEqual(recovered["status"], "scheduled")

    def test_locked_resource_conflict_and_permissions(self):
        flight1 = self.make_flight("AB101", "AC1", "CR1")
        disruption1 = self.svc.create_disruption("sched", "scheduler", {"kind": "crew_timeout", "resource_id": "CR1", "starts_at": iso(self.base), "ends_at": iso(self.base + timedelta(hours=2))})
        plan1 = self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption1["id"], "name": "方案一", "assignments": [{"flight_id": flight1["id"], "aircraft_id": "AC2", "crew_id": "CR2", "new_std": iso(self.base + timedelta(hours=2)), "new_sta": iso(self.base + timedelta(hours=4))}]})
        self.svc.lock_plan(plan1["id"], "ops", "ops_manager", {"expected_revision": 1})
        flight2 = self.make_flight("AB102", "AC2", "CR2")
        disruption2 = self.svc.create_disruption("sched", "scheduler", {"kind": "airport_closure", "resource_id": "AAA", "starts_at": iso(self.base), "ends_at": iso(self.base + timedelta(hours=1))})
        # 保存第二套方案时租约即冲突：晚到的方案看到占用方和空闲候选
        with self.assertRaises(ApiError) as ctx:
            self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption2["id"], "name": "冲突方案", "assignments": [{"flight_id": flight2["id"], "aircraft_id": "AC2", "crew_id": "CR2", "new_std": iso(self.base + timedelta(hours=2, minutes=30)), "new_sta": iso(self.base + timedelta(hours=4, minutes=30))}]})
        self.assertEqual(ctx.exception.code, "lease_conflict")
        details = ctx.exception.details
        self.assertEqual(len(details), 2)  # 飞机与机组各一条
        occupant = next(d for d in details if d["resource_type"] == "aircraft")
        self.assertEqual(occupant["occupant"]["plan_id"], plan1["id"])
        self.assertIn("AC1", occupant["free_candidates"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.lock_plan(plan1["id"], "sched", "scheduler", {"expected_revision": 1})
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx:
            self.svc.add_assignment(plan1["id"], "sched", "scheduler", {"expected_revision": 1, "flight_id": flight1["id"], "aircraft_id": "AC1", "crew_id": "CR1", "new_std": iso(self.base), "new_sta": iso(self.base + timedelta(hours=2))})
        self.assertEqual(ctx.exception.code, "plan_locked")

    def test_locked_resource_conflict_after_lease_expiry(self):
        # 租约过期后锁定报 locked_resource_conflict，带占用方和空闲候选
        flight = self.make_flight("AB103", "AC1", "CR1")
        disruption = self.svc.create_disruption("sched", "scheduler", {"kind": "aircraft_fault", "resource_id": "AC1", "starts_at": iso(self.base - timedelta(hours=1)), "ends_at": iso(self.base + timedelta(hours=2))})
        # 占位方案占用 AC2 + CR1
        other = self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": "占位方案", "lease_ttl_seconds": 3600,
                                                            "assignments": [{"flight_id": flight["id"], "aircraft_id": "AC2", "crew_id": "CR1", "new_std": iso(self.base + timedelta(hours=2)), "new_sta": iso(self.base + timedelta(hours=4))}]})
        # 晚到方案用另一航班、AC1+CR2，在 +3..+5 窗口（与占位 AC2/CR1 不重叠），1 秒短租约
        flight_b = self.make_flight("AB104", "AC1", "CR2")
        late = self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": "晚到方案", "lease_ttl_seconds": 1,
                                                           "assignments": [{"flight_id": flight_b["id"], "aircraft_id": "AC1", "crew_id": "CR2",
                                                                            "new_std": iso(self.base + timedelta(hours=3)), "new_sta": iso(self.base + timedelta(hours=5))}]})
        # 占位锁定；晚到方案 1 秒租约过期
        self.svc.lock_plan(other["id"], "ops", "ops_manager", {"expected_revision": 1})
        import time as _t; _t.sleep(1.1)
        # 晚到租约过期后，第三方抢入 AC1+CR2（与晚到时段重叠）
        flight_c = self.make_flight("AB105", "AC2", "CR1")
        third = self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": "抢入方案", "lease_ttl_seconds": 3600,
                                                            "assignments": [{"flight_id": flight_c["id"], "aircraft_id": "AC1", "crew_id": "CR2",
                                                                             "new_std": iso(self.base + timedelta(hours=3, minutes=30)), "new_sta": iso(self.base + timedelta(hours=5, minutes=30))}]})
        with self.assertRaises(ApiError) as ctx:
            self.svc.lock_plan(late["id"], "ops", "ops_manager", {"expected_revision": 1})
        self.assertEqual(ctx.exception.code, "locked_resource_conflict")
        by_type = {d["resource_type"]: d for d in ctx.exception.details}
        self.assertEqual(by_type["aircraft"]["occupant"]["plan_id"], third["id"])
        self.assertEqual(by_type["crew"]["occupant"]["plan_id"], third["id"])
        # AC1 被抢入、AC2 被锁定占位，飞机候选为空（晚到方看到全部占用方）
        self.assertEqual(by_type["aircraft"]["free_candidates"], [])


class LeaseTest(ScenarioCase):
    def make_disrupted_flight_plan(self, number="AB200", hours=0):
        flight = self.make_flight(number, "AC1", "CR1")
        disruption = self.svc.create_disruption("sched", "scheduler", {"kind": "aircraft_fault", "resource_id": "AC1", "starts_at": iso(self.base - timedelta(hours=1)), "ends_at": iso(self.base + timedelta(hours=2))})
        plan = self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": number + "方案", "lease_ttl_seconds": 3600,
                                                           "assignments": [{"flight_id": flight["id"], "aircraft_id": "AC2", "crew_id": "CR2",
                                                                            "new_std": iso(self.base + timedelta(hours=2 + hours)), "new_sta": iso(self.base + timedelta(hours=4 + hours))}]})
        return flight, disruption, plan

    def test_lease_overlap_single_valid_and_release(self):
        flight, disruption, plan = self.make_disrupted_flight_plan("AB201")
        leases = self.svc.get_plan(plan["id"])["leases"]
        self.assertEqual(len(leases), 2)
        self.assertTrue(all(l["valid"] and l["state"] == "held" for l in leases))
        # 第二航班在重叠时段只能看到占用
        flight_b = self.make_flight("AB202", "AC2", "CR2")
        with self.assertRaises(ApiError) as ctx:
            self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": "重叠方案",
                                                        "assignments": [{"flight_id": flight_b["id"], "aircraft_id": "AC2", "crew_id": "CR2",
                                                                         "new_std": iso(self.base + timedelta(hours=3)), "new_sta": iso(self.base + timedelta(hours=5))}]})
        self.assertEqual(ctx.exception.code, "lease_conflict")
        # 释放后资源恢复空闲，可以再租
        self.svc.release_plan_leases(plan["id"], "sched", "scheduler")
        again = self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": "再来一套",
                                                            "assignments": [{"flight_id": flight_b["id"], "aircraft_id": "AC2", "crew_id": "CR2",
                                                                             "new_std": iso(self.base + timedelta(hours=3)), "new_sta": iso(self.base + timedelta(hours=5))}]})
        self.assertEqual(len(again["leases"]), 2)

    def test_lease_expiry_renew_and_reacquire(self):
        _, _, plan = self.make_disrupted_flight_plan("AB203")
        # 1 秒短租约：过期后续约报 lease_lost，重新获取恢复
        short = self.svc.create_plan  # noqa
        import time as _t
        # 用 reacquire 换成 1 秒 TTL
        self.svc.reacquire_plan_leases(plan["id"], "sched", "scheduler", {"lease_ttl_seconds": 1})
        _t.sleep(1.1)
        with self.assertRaises(ApiError) as ctx:
            self.svc.renew_plan_leases(plan["id"], "sched", "scheduler", {"lease_ttl_seconds": 3600})
        self.assertEqual(ctx.exception.code, "lease_lost")
        got = self.svc.reacquire_plan_leases(plan["id"], "sched", "scheduler", {"lease_ttl_seconds": 3600})
        self.assertEqual(sum(1 for l in got["leases"] if l["valid"]), 2)
        renewed = self.svc.renew_plan_leases(plan["id"], "sched", "scheduler", {"lease_ttl_seconds": 3600})
        self.assertEqual(renewed["renewed"], 2)

    def test_window_update_invalidates_and_recomputes(self):
        flight, disruption, plan = self.make_disrupted_flight_plan("AB204")
        # 窗口从 +2h 延长到 +6h：旧方案立即失效、租约释放、航段顺延到 +6h、延误重算
        updated = self.svc.update_disruption_window(disruption["id"], "ops", "ops_manager",
                                                    {"expected_revision": 1, "starts_at": iso(self.base - timedelta(hours=1)), "ends_at": iso(self.base + timedelta(hours=6))})
        self.assertEqual(updated["disruption"]["revision"], 2)
        self.assertEqual(updated["invalidated_plans"][0]["plan_id"], plan["id"])
        shifted = updated["invalidated_plans"][0]["shifted_legs"][0]
        self.assertEqual(shifted["new_std"], iso(self.base + timedelta(hours=6)))
        self.assertEqual(shifted["delay_minutes"], 360)
        stale = self.svc.get_plan(plan["id"])
        self.assertEqual(stale["status"], "stale")
        self.assertTrue(all(not l["valid"] for l in stale["leases"]))
        # 旧方案立即失效：不能直接改派/锁定
        with self.assertRaises(ApiError) as ctx:
            self.svc.add_assignment(plan["id"], "sched", "scheduler", {"expected_revision": 2, "flight_id": flight["id"], "aircraft_id": "AC2", "crew_id": "CR2", "new_std": iso(self.base + timedelta(hours=6)), "new_sta": iso(self.base + timedelta(hours=8))})
        self.assertEqual(ctx.exception.code, "plan_stale")
        # 资源已释放，其他方案可立即占用
        flight_b = self.make_flight("AB205", "AC2", "CR2")
        other = self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": "新窗口方案",
                                                            "assignments": [{"flight_id": flight_b["id"], "aircraft_id": "AC2", "crew_id": "CR2",
                                                                             "new_std": iso(self.base + timedelta(hours=6)), "new_sta": iso(self.base + timedelta(hours=8))}]})
        self.assertEqual(other["leases"][0]["state"], "held")
        # 旧方案重新获取租约：需先释放新窗口方案（模拟其被否决/撤回），随后旧方案恢复 draft
        self.svc.release_plan_leases(other["id"], "sched", "scheduler")
        got = self.svc.reacquire_plan_leases(plan["id"], "sched", "scheduler", {"lease_ttl_seconds": 3600})
        self.assertEqual(got["plan"]["status"], "draft")


class TakeoverTest(ScenarioCase):
    def _takeover_setup(self):
        flight = self.make_flight("AB300", "AC1", "CR1")
        disruption = self.svc.create_disruption("sched", "scheduler", {"kind": "aircraft_fault", "resource_id": "AC1", "starts_at": iso(self.base - timedelta(hours=1)), "ends_at": iso(self.base + timedelta(hours=2))})
        locked_plan = self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": "先锁定方案", "lease_ttl_seconds": 3600,
                                                                  "assignments": [{"flight_id": flight["id"], "aircraft_id": "AC2", "crew_id": "CR2",
                                                                                   "new_std": iso(self.base + timedelta(hours=2)), "new_sta": iso(self.base + timedelta(hours=4)), "missed_connections": 3}]})
        self.svc.lock_plan(locked_plan["id"], "ops", "ops_manager", {"expected_revision": 1})
        return flight, disruption, locked_plan

    def test_takeover_flow_and_diff(self):
        flight, disruption, locked_plan = self._takeover_setup()
        # 窗口延长到 +5h
        self.svc.update_disruption_window(disruption["id"], "ops", "ops_manager",
                                          {"expected_revision": 1, "starts_at": iso(self.base - timedelta(hours=1)), "ends_at": iso(self.base + timedelta(hours=5))})
        marked = self.svc.get_plan(locked_plan["id"])
        self.assertTrue(marked["window_stale"])
        # 调度员为同一中断准备接管方案（intended 租约，不真正占用 AC2/CR2）
        takeover = self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": "接管方案", "takeover_target_id": locked_plan["id"],
                                                               "assignments": [{"flight_id": flight["id"], "aircraft_id": "AC2", "crew_id": "CR2",
                                                                                "new_std": iso(self.base + timedelta(hours=5)), "new_sta": iso(self.base + timedelta(hours=7))}]})
        self.assertTrue(all(l["state"] == "intended" for l in takeover["leases"]))
        # 接管方案不能直接锁定
        with self.assertRaises(ApiError) as ctx:
            self.svc.lock_plan(takeover["id"], "ops", "ops_manager", {"expected_revision": 1})
        self.assertEqual(ctx.exception.code, "takeover_required")
        # scheduler 无权接管
        with self.assertRaises(ApiError) as ctx:
            self.svc.takeover_plan(takeover["id"], "sched", "scheduler", {"target_plan_id": locked_plan["id"]})
        self.assertEqual(ctx.exception.status, 403)
        # 运行经理执行接管：按最新窗口重算延误、移交租约
        result = self.svc.takeover_plan(takeover["id"], "ops", "ops_manager", {"target_plan_id": locked_plan["id"]})
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["plan"]["status"], "locked")
        self.assertEqual(result["diff"]["window_revision"], 2)
        leg = result["diff"]["legs"][0]
        self.assertEqual(leg["new_delay_minutes"], 300)
        self.assertEqual(leg["target_delay_minutes"], 120)
        self.assertEqual(result["diff"]["total_delay_delta_minutes"], 180)
        # 旧方案被取代，其永久租约 superseded
        self.assertEqual(self.svc.get_plan(locked_plan["id"])["status"], "superseded")
        active = [l for l in self.svc.list_leases()["leases"] if l["valid"] and l["state"] == "held"]
        self.assertTrue(all(l["plan_id"] == takeover["id"] for l in active))
        # 航班已写入最新时刻
        updated_flight = self.svc.state()["flights"][0]
        self.assertEqual(updated_flight["std"], iso(self.base + timedelta(hours=5)))
        self.assertEqual(updated_flight["delay_minutes"], 300)
        # 控制台留存接管差异
        history = self.svc.list_takeovers(disruption["id"])["takeovers"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["diff"]["taking_plan_id"], takeover["id"])

    def test_takeover_retries_after_write_failure_and_keeps_original_leases(self):
        flight, disruption, locked_plan = self._takeover_setup()
        takeover = self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": "接管方案B", "takeover_target_id": locked_plan["id"],
                                                               "assignments": [{"flight_id": flight["id"], "aircraft_id": "AC2", "crew_id": "CR2",
                                                                                "new_std": iso(self.base + timedelta(hours=2)), "new_sta": iso(self.base + timedelta(hours=4))}]})
        rollback_seen = []
        self.svc.repo.fail_commits_remaining = 1  # 第一次提交失败
        self.svc.repo.on_rollback = lambda: rollback_seen.append(1)
        result = self.svc.takeover_plan(takeover["id"], "ops", "ops_manager", {"target_plan_id": locked_plan["id"]})
        self.assertEqual(result["attempts"], 2)  # 第一次失败回滚，第二次成功
        self.assertEqual(len(rollback_seen), 1)
        # 回滚期间原租约未被破坏；最终状态为接管成功
        self.assertEqual(self.svc.get_plan(locked_plan["id"])["status"], "superseded")
        self.assertEqual(self.svc.get_plan(takeover["id"])["status"], "locked")
        history = self.svc.list_takeovers()["takeovers"]
        self.assertEqual(history[0]["attempts"], 2)

    def test_takeover_blocked_by_third_plan_lease(self):
        flight, disruption, locked_plan = self._takeover_setup()
        # 窗口延长到 +5h
        self.svc.update_disruption_window(disruption["id"], "ops", "ops_manager",
                                          {"expected_revision": 1, "starts_at": iso(self.base - timedelta(hours=1)), "ends_at": iso(self.base + timedelta(hours=5))})
        # 第三方在锁定方案窗口之后（+5..+7）持有 AC2；目标重算后接管航段会落到 +5..+7，正好冲突
        flight_b = self.make_flight("AB301", "AC1", "CR2")
        third = self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": "第三方",
                                                            "assignments": [{"flight_id": flight_b["id"], "aircraft_id": "AC2", "crew_id": "CR2",
                                                                             "new_std": iso(self.base + timedelta(hours=5)), "new_sta": iso(self.base + timedelta(hours=7))}]})
        takeover = self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": "接管方案C", "takeover_target_id": locked_plan["id"],
                                                               "assignments": [{"flight_id": flight["id"], "aircraft_id": "AC2", "crew_id": "CR2",
                                                                                "new_std": iso(self.base + timedelta(hours=2)), "new_sta": iso(self.base + timedelta(hours=4))}]})
        with self.assertRaises(ApiError) as ctx:
            self.svc.takeover_plan(takeover["id"], "ops", "ops_manager", {"target_plan_id": locked_plan["id"]})
        self.assertEqual(ctx.exception.code, "lease_conflict")
        detail = next(d for d in ctx.exception.details if d["resource_type"] == "aircraft")
        self.assertEqual(detail["occupant"]["plan_id"], third["id"])
        self.assertIn("AC1", detail["free_candidates"])
        # 接管未生效：目标仍锁定
        self.assertEqual(self.svc.get_plan(locked_plan["id"])["status"], "locked")
        self.svc.release_plan_leases(third["id"], "sched", "scheduler")
        result = self.svc.takeover_plan(takeover["id"], "ops", "ops_manager", {"target_plan_id": locked_plan["id"]})
        self.assertEqual(result["plan"]["status"], "locked")


if __name__ == "__main__": unittest.main()
