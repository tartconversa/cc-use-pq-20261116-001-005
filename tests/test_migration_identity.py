import tempfile
import unittest
from pathlib import Path

from exhibit_lake.errors import Conflict, InvalidInput, NotFound
from exhibit_lake.service import ExhibitLake


SHANGHAI = "Asia/Shanghai"
NEW_YORK = "America/New_York"


class MigrationTests(unittest.TestCase):
    """有生效日期的场馆迁移与展品身份映射。"""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.lake = ExhibitLake(Path(self.folder.name) / "lake.db")
        self.lake.register_venue("showroom", "Showroom", SHANGHAI)
        self.lake.register_venue("museum", "Museum", NEW_YORK)
        self.lake.register_exhibit("robot", "showroom", "Robot", "Maker", "2026-01-01")
        for source_id, venue_id in [("show-visits", "showroom"), ("museum-visits", "museum")]:
            self.lake.register_source(source_id, venue_id, "visit")
            self.lake.register_contract(source_id + "-v1", source_id,
                                        ["event_id", "exhibit_id", "event_at", "count"],
                                        "2026-01-01T00:00:00+00:00")

    def visit(self, event_id, at, count=1, exhibit_id="robot"):
        return {"event_id": event_id, "exhibit_id": exhibit_id, "event_at": at, "count": count}

    def migrate(self, effective_on="2026-03-10", migration_id="m1", to_venue="museum"):
        return self.lake.migrate_exhibit(migration_id, "robot", to_venue, effective_on)

    # ---- 生效日与落馆 ----

    def test_placement_split_is_effective_dated_and_contiguous(self):
        result = self.migrate()
        self.assertEqual(result["from_venue_id"], "showroom")
        self.assertEqual(result["to_venue_id"], "museum")
        placements = self.lake.placements.placements("robot")
        self.assertEqual(len(placements), 2)
        first, second = placements
        self.assertEqual(first["venue_id"], "showroom")
        self.assertEqual(first["effective_to"], result["cutover_at"])
        self.assertEqual(second["venue_id"], "museum")
        self.assertEqual(second["effective_from"], result["cutover_at"])
        self.assertIsNone(second["effective_to"])
        with self.lake.store.transaction(write=False) as conn:
            self.assertEqual(self.lake.placements.venue_at(conn, "robot", result["cutover_at"]), "museum")

    def test_event_before_migration_stays_with_source_venue(self):
        self.migrate()
        batch = self.lake.ingest("show-visits", "show-visits-v1", "before",
                                 [self.visit("e1", "2026-03-09T18:00:00+08:00", 4)])
        self.assertEqual(batch["valid_rows"], 1)
        self.assertEqual(self.lake.snapshots.inventory("showroom", "2026-03-09")["sources"][0]["events"], 1)
        self.assertEqual(self.lake.snapshots.inventory("museum", "2026-03-09")["sources"], [])

    def test_source_contract_from_old_venue_is_rejected_after_migration(self):
        self.migrate()
        batch = self.lake.ingest("show-visits", "show-visits-v1", "after",
                                 [self.visit("e2", "2026-03-11T12:00:00+08:00")])
        self.assertEqual(batch["valid_rows"], 0)
        rejection = batch["rejections"][0]
        self.assertEqual(rejection["code"], "outside_source_venue")
        self.assertEqual(rejection["exhibit_venue_id"], "museum")
        self.assertEqual(rejection["source_venue_id"], "showroom")

    def test_target_venue_local_day_partition_after_migration(self):
        self.migrate()
        batch = self.lake.ingest("museum-visits", "museum-visits-v1", "ny",
                                 [self.visit("e3", "2026-03-11T23:30:00-04:00", 6)])
        self.assertEqual(batch["valid_rows"], 1)
        # 纽约 3 月 11 日 23:30 在上海已是 3 月 12 日；分区必须按目标馆时区落位。
        self.assertEqual(self.lake.snapshots.inventory("museum", "2026-03-11")["sources"][0]["events"], 1)
        self.assertEqual(self.lake.snapshots.inventory("museum", "2026-03-12")["sources"], [])

    def test_history_before_migration_is_not_relabelled(self):
        self.lake.ingest("show-visits", "show-visits-v1", "before",
                         [self.visit("e1", "2026-03-05T12:00:00+08:00", 5)])
        snapshot = self.lake.publish_day("showroom", "2026-03-05")
        self.migrate()
        # 已发布快照与原事件归属保持不变。
        self.assertEqual(self.lake.analytics.day("showroom", "2026-03-05")["total_visitors"], 5)
        self.assertEqual(snapshot["snapshot_id"],
                         self.lake.snapshots.head("showroom", "2026-03-05")["head_id"])
        with self.lake.store.transaction(write=False) as conn:
            row = self.lake.store.one(conn, "SELECT venue_id,local_day FROM accepted_events WHERE event_id='e1'")
        self.assertEqual((row["venue_id"], row["local_day"]), ("showroom", "2026-03-05"))
        self.assertEqual(self.lake.snapshots.inventory("museum", "2026-03-05")["sources"], [])

    def test_published_event_after_cutover_blocks_backdated_migration(self):
        self.lake.ingest("show-visits", "show-visits-v1", "late",
                         [self.visit("e9", "2026-03-15T12:00:00+08:00", 2)])
        self.lake.publish_day("showroom", "2026-03-15")
        with self.assertRaises(Conflict):
            self.migrate(effective_on="2026-03-10")

    def test_staging_rejects_partition_that_no_longer_matches_layout(self):
        self.lake.ingest("show-visits", "show-visits-v1", "pending",
                         [self.visit("e7", "2026-03-11T12:00:00+08:00", 3)])
        self.migrate()
        with self.assertRaises(Conflict):
            self.lake.snapshots.stage("showroom", "2026-03-11")

    # ---- 冲突与幂等 ----

    def test_overlapping_placement_is_rejected(self):
        self.migrate()
        with self.assertRaises(Conflict):
            self.lake.migrate_exhibit("m2", "robot", "showroom", "2026-03-10")
        with self.assertRaises(Conflict):
            self.lake.migrate_exhibit("m3", "robot", "showroom", "2026-03-01")

    def test_duplicate_migration_id_conflicts(self):
        self.migrate()
        with self.assertRaises(Conflict):
            self.lake.migrate_exhibit("m1", "robot", "museum", "2026-03-20")

    def test_request_key_is_idempotent(self):
        first = self.lake.migrate_exhibit("m1", "robot", "museum", "2026-03-10", request_key="key")
        second = self.lake.migrate_exhibit("m1", "robot", "museum", "2026-03-10", request_key="key")
        self.assertTrue(second["replay"])
        self.assertEqual(first["cutover_at"], second["cutover_at"])
        with self.assertRaises(Conflict):
            self.lake.migrate_exhibit("m9", "robot", "museum", "2026-03-10", request_key="key")

    def test_migration_before_launch_or_after_withdrawal_is_invalid(self):
        with self.assertRaises(InvalidInput):
            self.migrate(effective_on="2025-12-31")
        self.lake.registry.withdraw("robot", "2026-02-01")
        with self.assertRaises(InvalidInput):
            self.migrate(effective_on="2026-03-10")

    def test_unknown_target_venue_is_not_found(self):
        with self.assertRaises(NotFound):
            self.lake.migrate_exhibit("m1", "robot", "missing", "2026-03-10")

    def test_same_venue_migration_conflicts(self):
        with self.assertRaises(Conflict):
            self.migrate(to_venue="showroom")

    # ---- 身份映射 ----

    def test_alias_resolves_legacy_local_identity_to_global_exhibit(self):
        self.migrate()
        self.lake.register_exhibit_alias("R-001", "robot", "2026-03-10")
        batch = self.lake.ingest("museum-visits", "museum-visits-v1", "alias",
                                 [self.visit("a1", "2026-03-12T12:00:00-04:00", 3, exhibit_id="R-001")])
        self.assertEqual(batch["valid_rows"], 1)
        with self.lake.store.transaction(write=False) as conn:
            row = self.lake.store.one(conn, "SELECT exhibit_id,requested_exhibit_id,venue_id "
                                             "FROM accepted_events WHERE event_id='a1'")
        self.assertEqual(row["exhibit_id"], "robot")
        self.assertEqual(row["requested_exhibit_id"], "R-001")
        self.assertEqual(row["venue_id"], "museum")
        resolved = self.lake.resolve_exhibit("R-001", "2026-03-12T12:00:00-04:00")
        self.assertEqual(resolved["exhibit_id"], "robot")
        self.assertTrue(resolved["alias"])

    def test_alias_window_prevents_reinterpreting_history(self):
        self.lake.register_exhibit_alias("R-001", "robot", "2026-03-10")
        with self.assertRaises(NotFound):
            self.lake.resolve_exhibit("R-001", "2026-03-01T12:00:00+08:00")

    def test_alias_before_effective_window_is_unknown(self):
        self.lake.register_exhibit_alias("R-001", "robot", "2026-03-10")
        self.lake.register_exhibit("sensor-x", "museum", "Sensor", "Maker", "2026-01-01")
        self.lake.register_source("museum-feedback", "museum", "feedback")
        self.lake.register_contract("museum-feedback-v1", "museum-feedback",
                                    ["event_id", "exhibit_id", "event_at", "rating", "visitor_id"],
                                    "2026-01-01T00:00:00+00:00")
        batch = self.lake.ingest("museum-feedback", "museum-feedback-v1", "early",
                                 [{"event_id": "f1", "exhibit_id": "R-001",
                                   "event_at": "2026-03-09T00:00:00-05:00", "rating": 5, "visitor_id": "v"}])
        self.assertEqual(batch["rejections"][0]["code"], "unknown_exhibit")

    def test_alias_retirement_stops_future_resolution(self):
        self.lake.register_exhibit_alias("R-001", "robot", "2026-01-01")
        retired = self.lake.retire_exhibit_alias("R-001", "2026-03-10")
        self.assertEqual(retired["effective_to"], "2026-03-09")
        with self.assertRaises(NotFound):
            self.lake.resolve_exhibit("R-001", "2026-03-10T12:00:00+08:00")

    def test_alias_cannot_collide_with_existing_identity(self):
        self.lake.register_exhibit("sensor-x", "museum", "Sensor", "Maker", "2026-01-01")
        with self.assertRaises(Conflict):
            self.lake.register_exhibit_alias("robot", "sensor-x", "2026-01-01")

    # ---- 同日双馆上报 ----

    def test_same_day_reports_from_both_venues_are_reported_as_conflict(self):
        self.lake.ingest("show-visits", "show-visits-v1", "old",
                         [self.visit("old", "2026-03-11T12:00:00+08:00", 2)])
        self.migrate()
        # 来源不匹配，旧馆行保留为待重放；此时同日两馆都有未发布事件。
        self.lake.ingest("museum-visits", "museum-visits-v1", "new",
                         [self.visit("new", "2026-03-11T12:00:00-04:00", 3)])
        conflicts = self.lake.placements.conflicts("robot")
        self.assertEqual(conflicts["conflict_count"], 1)
        day = conflicts["same_day_multi_venue"][0]
        self.assertEqual(day["local_day"], "2026-03-11")
        self.assertEqual(day["venue_ids"], ["museum", "showroom"])
        self.assertEqual({x["event_id"] for x in day["events"]}, {"old", "new"})

    def test_no_conflict_after_published_events_are_ignored(self):
        self.lake.ingest("show-visits", "show-visits-v1", "old",
                         [self.visit("old", "2026-03-09T12:00:00+08:00", 2)])
        self.lake.publish_day("showroom", "2026-03-09")
        self.migrate()
        self.lake.ingest("museum-visits", "museum-visits-v1", "new",
                         [self.visit("new", "2026-03-11T12:00:00-04:00", 3)])
        conflicts = self.lake.placements.conflicts("robot")
        self.assertEqual(conflicts["overlaps"], [])
        self.assertEqual(conflicts["same_day_multi_venue"], [])

    # ---- 撤回 ----

    def test_retraction_restores_layout_and_preserves_published_snapshot(self):
        self.lake.ingest("museum-visits", "museum-visits-v1", "ny",
                         [self.visit("e4", "2026-03-11T12:00:00-04:00", 7)])
        self.lake.publish_day("museum", "2026-03-11")
        self.migrate()
        # 迁移后无法再通过来源契约入住旧馆；先构造迁移后再发布的一笔。
        self.lake.ingest("museum-visits", "museum-visits-v1", "ny2",
                         [self.visit("e5", "2026-03-13T12:00:00-04:00", 4)])
        snap = self.lake.publish_day("museum", "2026-03-13")
        result = self.lake.retract_migration("m1", "2026-03-15")
        self.assertEqual(result["status"], "retracted")
        self.assertEqual([x["snapshot_id"] for x in result["preserved_snapshots"]], [snap["snapshot_id"]])
        self.assertEqual(self.lake.analytics.day("museum", "2026-03-13", snap["snapshot_id"])["total_visitors"], 4)
        venues = {p["venue_id"]: p for p in self.lake.placements.placements("robot")}
        self.assertIsNone(venues["showroom"]["effective_to"])
        # 撤回后目标驻留归零并被移除，布局回到来源馆单段。
        self.assertNotIn("museum", venues)
        with self.assertRaises(Conflict):
            self.lake.retract_migration("m1", "2026-03-16")

    def test_retraction_requires_existing_migration(self):
        with self.assertRaises(NotFound):
            self.lake.retract_migration("missing", "2026-03-15")

    def test_retraction_before_effective_day_is_invalid(self):
        self.migrate("2026-03-10")
        with self.assertRaises(InvalidInput):
            self.lake.retract_migration("m1", "2026-03-01")

    def test_retraction_reports_pending_events_to_replay(self):
        self.migrate()
        self.lake.ingest("museum-visits", "museum-visits-v1", "ny",
                         [self.visit("e6", "2026-03-12T12:00:00-04:00", 2)])
        result = self.lake.retract_migration("m1", "2026-03-15")
        # museum 来源契约无法自动改投 showroom，事件保持原行并明确要求重放。
        self.assertEqual(result["pending_events_requeued"], 0)
        self.assertEqual(result["pending_events_requiring_replay"], 1)
        self.assertEqual(result["pending_event_keys"][0]["event_id"], "e6")
        with self.lake.store.transaction(write=False) as conn:
            row = self.lake.store.one(conn, "SELECT venue_id,local_day FROM accepted_events WHERE event_id='e6'")
        self.assertEqual((row["venue_id"], row["local_day"]), ("museum", "2026-03-12"))

    def test_later_migration_blocks_retraction(self):
        self.migrate("2026-03-10", "m1")
        self.lake.migrate_exhibit("m2", "robot", "showroom", "2026-03-20")
        with self.assertRaises(Conflict):
            self.lake.retract_migration("m1", "2026-03-25")
        self.assertEqual(self.lake.retract_migration("m2", "2026-03-25")["status"], "retracted")

    # ---- 审计 ----

    def test_audit_trail_records_migration_and_retraction(self):
        self.migrate()
        self.lake.retract_migration("m1", "2026-03-15")
        trail = self.lake.audit.trail(limit=100)["audits"]
        actions = [x["action"] for x in trail]
        self.assertIn("exhibit.migrated", actions)
        self.assertIn("exhibit.migration_retracted", actions)
        migrated = next(x for x in trail if x["action"] == "exhibit.migrated")
        self.assertEqual(migrated["subject"], "robot")
        self.assertEqual(migrated["detail"]["to_venue_id"], "museum")

    def test_conflicts_report_is_clean_for_consistent_layout(self):
        self.migrate()
        result = self.lake.placements.conflicts("robot")
        self.assertEqual(result["conflict_count"], 0)
        self.assertEqual(len(result["placements"]), 2)
        self.assertEqual(result["migrations"][0]["status"], "active")


if __name__ == "__main__":
    unittest.main()
