import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import AirlineRecoveryService, ApiError, iso, utcnow


class ResourceLeaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = AirlineRecoveryService(Path(self.tmp.name) / "test.db")
        base = utcnow() + timedelta(days=1)
        self.svc.seed_airport("ops", "ops_manager", {"code": "AAA", "country": "CN", "curfew_start": "23:00", "curfew_end": "05:00"})
        self.svc.seed_airport("ops", "ops_manager", {"code": "BBB", "country": "CN", "curfew_start": "23:00", "curfew_end": "05:00"})
        for ac in ("AC1", "AC2", "AC3"):
            self.svc.seed_aircraft("ops", "ops_manager", {"id": ac, "model": "A320", "maintenance_due": iso(base + timedelta(days=5))})
        for cr in ("CR1", "CR2", "CR3"):
            self.svc.seed_crew("ops", "ops_manager", {"id": cr, "name": cr, "base": "AAA", "duty_start": iso(base - timedelta(hours=2)), "max_duty_minutes": 720})
        self.svc.create_permit("ops", "ops_manager", {"origin": "AAA", "destination": "BBB", "valid_from": iso(base - timedelta(days=1)), "valid_to": iso(base + timedelta(days=2))})
        self.base = base

    def tearDown(self):
        self.tmp.cleanup()

    def make_flight(self, number, aircraft, crew):
        return self.svc.create_flight("sched", "scheduler", {"flight_no": number, "origin": "AAA", "destination": "BBB", "std": iso(self.base), "sta": iso(self.base + timedelta(hours=2)), "aircraft_id": aircraft, "crew_id": crew, "passenger_count": 150})

    def make_plan(self, flight, aircraft, crew, start_offset_h, end_offset_h, disruption=None):
        if disruption is None:
            disruption = self.svc.create_disruption("sched", "scheduler", {"kind": "aircraft_fault", "resource_id": aircraft, "starts_at": iso(self.base), "ends_at": iso(self.base + timedelta(hours=1))})
        return self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": f"方案-{aircraft}", "assignments": [
            {"flight_id": flight["id"], "aircraft_id": aircraft, "crew_id": crew,
             "new_std": iso(self.base + timedelta(hours=start_offset_h)), "new_sta": iso(self.base + timedelta(hours=end_offset_h))}]})

    def test_leases_acquired_on_save_and_released_on_takeover(self):
        flight = self.make_flight("AB100", "AC1", "CR1")
        plan = self.make_plan(flight, "AC2", "CR2", 2, 4)
        leases = self.svc.list_plan_leases(plan["id"])["leases"]
        self.assertEqual({(l["resource_type"], l["resource_id"]) for l in leases}, {("aircraft", "AC2"), ("crew", "CR2")})
        self.assertTrue(all(l["status"] == "active" for l in leases))
        # 不重叠时段可以同时持有。
        flight2 = self.make_flight("AB101", "AC3", "CR3")
        plan2 = self.make_plan(flight2, "AC2", "CR2", 5, 7)
        self.assertEqual(len(self.svc.list_plan_leases(plan2["id"])["leases"]), 2)

    def test_overlapping_save_shows_occupant_and_candidates(self):
        flight = self.make_flight("AB102", "AC1", "CR1")
        plan1 = self.make_plan(flight, "AC2", "CR2", 2, 4)
        flight2 = self.make_flight("AB103", "AC3", "CR3")
        with self.assertRaises(ApiError) as ctx:
            self.make_plan(flight2, "AC2", "CR2", 2, 4)
        err = ctx.exception
        self.assertEqual(err.code, "lease_conflict")
        self.assertEqual(err.details["occupant"]["plan_id"], plan1["id"])
        self.assertEqual(err.details["occupant"]["plan_name"], plan1["name"])
        self.assertIn("AC1", [c["id"] for c in err.details["candidates"]])
        # 冲突方案没有被落库。
        self.assertEqual(len(self.svc.state()["plans"]), 1)

    def test_renew_extends_lease(self):
        flight = self.make_flight("AB104", "AC1", "CR1")
        plan = self.make_plan(flight, "AC2", "CR2", 2, 4)
        created = self.svc.list_plan_leases(plan["id"])["leases"][0]["created_at"]
        renewed = self.svc.renew_plan_leases(plan["id"], "sched", "scheduler")
        self.assertEqual(renewed["renewed"], 2)
        self.assertGreater(renewed["expires_at"], created)

    def test_reassignment_releases_old_leases(self):
        flight = self.make_flight("AB113", "AC1", "CR1")
        plan = self.make_plan(flight, "AC2", "CR2", 2, 4)
        # 改派到 AC3/CR3：旧租约应释放，新租约建立。
        self.svc.add_assignment(plan["id"], "sched", "scheduler", {"expected_revision": 1, "flight_id": flight["id"],
            "aircraft_id": "AC3", "crew_id": "CR3", "new_std": iso(self.base + timedelta(hours=2)), "new_sta": iso(self.base + timedelta(hours=4))})
        leases = self.svc.list_plan_leases(plan["id"])["leases"]
        active = {(l["resource_type"], l["resource_id"]) for l in leases if l["status"] == "active"}
        self.assertEqual(active, {("aircraft", "AC3"), ("crew", "CR3")})
        self.assertTrue(all(l["status"] == "released" for l in leases if l["resource_id"] in ("AC2", "CR2")))
        # AC2 已空闲，可被其它方案在同一时段租用。
        flight2 = self.make_flight("AB114", "AC1", "CR1")
        plan2 = self.make_plan(flight2, "AC2", "CR2", 2, 4)
        self.assertEqual(len(self.svc.list_plan_leases(plan2["id"])["leases"]), 2)

    def test_expired_lease_does_not_block(self):
        flight = self.make_flight("AB105", "AC1", "CR1")
        plan1 = self.make_plan(flight, "AC2", "CR2", 2, 4)
        # 手工把租约置为过期。
        self.svc.repo.conn.execute("UPDATE resource_leases SET expires_at=? WHERE plan_id=?", ("2000-01-01T00:00:00Z", plan1["id"]))
        flight2 = self.make_flight("AB106", "AC3", "CR3")
        plan2 = self.make_plan(flight2, "AC2", "CR2", 2, 4)
        self.assertEqual(len(self.svc.list_plan_leases(plan2["id"])["leases"]), 2)

    def test_disruption_window_update_invalidates_plans(self):
        flight = self.make_flight("AB107", "AC1", "CR1")
        plan = self.make_plan(flight, "AC2", "CR2", 2, 4)
        disruption = self.svc.create_disruption("sched", "scheduler", {"kind": "aircraft_fault", "resource_id": "AC2", "starts_at": iso(self.base), "ends_at": iso(self.base + timedelta(hours=1))})
        # 把方案挂到该中断上。
        self.svc.repo.conn.execute("UPDATE recovery_plans SET disruption_id=? WHERE id=?", (disruption["id"], plan["id"]))
        result = self.svc.update_disruption_window(disruption["id"], "sched", "scheduler", {"starts_at": iso(self.base + timedelta(hours=1)), "ends_at": iso(self.base + timedelta(hours=3))})
        self.assertEqual(result["invalidated_plans"][0]["plan_id"], plan["id"])
        stale = self.svc.get_plan(plan["id"])
        self.assertEqual(stale["status"], "stale")
        self.assertTrue(all(l["status"] == "released" for l in stale["leases"]))

    def test_takeover_supersedes_locked_plan_and_records_diff(self):
        flight = self.make_flight("AB108", "AC1", "CR1")
        plan = self.make_plan(flight, "AC2", "CR2", 2, 4)
        self.svc.lock_plan(plan["id"], "ops", "ops_manager", {"expected_revision": 1})
        taken = self.svc.takeover_plan(plan["id"], "ops", "ops_manager", {"expected_revision": 1})
        self.assertEqual(taken["status"], "locked")
        self.assertEqual(taken["takeover_of"], plan["id"])
        # 原方案被接管。
        self.assertEqual(self.svc.get_plan(plan["id"])["status"], "superseded")
        # 租约转移：原方案 taken_over，接管方案 active。
        old_leases = self.svc.list_plan_leases(plan["id"])["leases"]
        new_leases = self.svc.list_plan_leases(taken["id"])["leases"]
        self.assertTrue(all(l["status"] == "taken_over" for l in old_leases))
        self.assertTrue(all(l["status"] == "active" for l in new_leases))
        # 航班时刻已按接管方案更新。
        flight_row = self.svc.repo.conn.execute("SELECT std,sta,aircraft_id,crew_id FROM flights WHERE id=?", (flight["id"],)).fetchone()
        self.assertEqual(flight_row["aircraft_id"], "AC2")
        # 控制台留存接管差异。
        audits = [dict(r) for r in self.svc.repo.conn.execute("SELECT action,detail_json FROM audit_log WHERE plan_id=? ORDER BY id", (taken["id"],)).fetchall()]
        self.assertEqual(audits[-1]["action"], "plan_takeover")
        diff = __import__("json").loads(audits[-1]["detail_json"])["diff"]
        self.assertEqual(diff[0]["flight_no"], "AB108")

    def test_takeover_with_override_uses_new_resource(self):
        flight = self.make_flight("AB109", "AC1", "CR1")
        plan = self.make_plan(flight, "AC2", "CR2", 2, 4)
        self.svc.lock_plan(plan["id"], "ops", "ops_manager", {"expected_revision": 1})
        taken = self.svc.takeover_plan(plan["id"], "ops", "ops_manager", {"expected_revision": 1, "assignments": [
            {"flight_id": flight["id"], "aircraft_id": "AC3", "crew_id": "CR3",
             "new_std": iso(self.base + timedelta(hours=3)), "new_sta": iso(self.base + timedelta(hours=5))}]})
        self.assertEqual(taken["assignments"][0]["aircraft_id"], "AC3")
        self.assertEqual(taken["assignments"][0]["delay_minutes"], 60)
        flight_row = self.svc.repo.conn.execute("SELECT aircraft_id,crew_id FROM flights WHERE id=?", (flight["id"],)).fetchone()
        self.assertEqual(flight_row["aircraft_id"], "AC3")

    def test_takeover_conflict_rolls_back_to_original_lease(self):
        flight = self.make_flight("AB110", "AC1", "CR1")
        plan = self.make_plan(flight, "AC2", "CR2", 2, 4)
        self.svc.lock_plan(plan["id"], "ops", "ops_manager", {"expected_revision": 1})
        # 让原租约过期，使第三方能占用同一时段。
        self.svc.repo.conn.execute("UPDATE resource_leases SET expires_at=? WHERE plan_id=?", ("2000-01-01T00:00:00Z", plan["id"]))
        flight2 = self.make_flight("AB111", "AC3", "CR3")
        self.make_plan(flight2, "AC2", "CR2", 2, 4)
        # 接管写入失败：回到原租约重试后仍冲突，原方案保持锁定、租约恢复。
        with self.assertRaises(ApiError) as ctx:
            self.svc.takeover_plan(plan["id"], "ops", "ops_manager", {"expected_revision": 1})
        self.assertEqual(ctx.exception.code, "lease_conflict")
        self.assertEqual(self.svc.get_plan(plan["id"])["status"], "locked")
        restored = self.svc.list_plan_leases(plan["id"])["leases"]
        self.assertTrue(all(l["status"] == "active" for l in restored))
        # 接管方案未落库。
        self.assertIsNone(self.svc.repo.conn.execute("SELECT id FROM recovery_plans WHERE takeover_of=?", (plan["id"],)).fetchone())

    def test_takeover_requires_ops_manager(self):
        flight = self.make_flight("AB112", "AC1", "CR1")
        plan = self.make_plan(flight, "AC2", "CR2", 2, 4)
        self.svc.lock_plan(plan["id"], "ops", "ops_manager", {"expected_revision": 1})
        with self.assertRaises(ApiError) as ctx:
            self.svc.takeover_plan(plan["id"], "sched", "scheduler", {"expected_revision": 1})
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
