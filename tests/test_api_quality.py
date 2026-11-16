import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from exhibit_lake.api import create_app


class ApiQualityTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.client = TestClient(create_app(str(Path(self.folder.name) / "api.db")))
        self.assertEqual(self.client.post("/venues", json={"venue_id": "venue", "name": "Venue",
                                                    "timezone": "Asia/Shanghai"}).status_code, 201)
        self.assertEqual(self.client.post("/exhibits", json={"exhibit_id": "robot", "venue_id": "venue",
                                                      "name": "Robot", "maker": "Maker", "launched_on": "2026-01-01"}).status_code, 201)
        self.assertEqual(self.client.post("/sources", json={"source_id": "counter", "venue_id": "venue",
                                                     "kind": "visit"}).status_code, 201)
        self.assertEqual(self.client.post("/contracts", json={"contract_id": "v1", "source_id": "counter",
                                                       "fields": ["event_id", "exhibit_id", "event_at", "count"],
                                                       "effective_from": "2026-01-01T00:00:00+00:00"}).status_code, 201)

    def row(self, event_id="v1", count=5):
        return {"event_id": event_id, "exhibit_id": "robot", "event_at": "2026-03-01T10:00:00+08:00", "count": count}

    def ingest(self, key="batch", rows=None):
        return self.client.post("/batches", json={"source_id": "counter", "contract_id": "v1",
                                                 "request_key": key, "events": rows if rows is not None else [self.row()]})

    def publish(self):
        staged = self.client.post("/snapshots/stage", json={"venue_id": "venue", "local_day": "2026-03-01"})
        self.assertEqual(staged.status_code, 201, staged.text)
        snapshot = staged.json()["snapshot_id"]
        result = self.client.post(f"/snapshots/{snapshot}/publish", json={"expected_head": None})
        self.assertEqual(result.status_code, 200, result.text)
        return snapshot

    def test_health(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})

    def test_duplicate_venue_error_envelope(self):
        result = self.client.post("/venues", json={"venue_id": "venue", "name": "Again", "timezone": "UTC"})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()["error"]["code"], "conflict")

    def test_unknown_venue_error_envelope(self):
        result = self.client.get("/venues/missing")
        self.assertEqual(result.status_code, 404)
        self.assertEqual(result.json()["error"]["code"], "not_found")

    def test_invalid_contract_fields_error(self):
        result = self.client.post("/contracts", json={"contract_id": "bad", "source_id": "counter",
                                                       "fields": ["event_id"], "effective_from": "2026-01-01T00:00:00+00:00"})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(result.json()["error"]["code"], "invalid_input")

    def test_preview_before_ingest(self):
        result = self.client.post("/batches/preview", json={"source_id": "counter", "contract_id": "v1",
                                                              "request_key": "batch", "events": [self.row()]})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["would_accept"], 1)
        self.assertEqual(self.client.get("/partitions/venue/2026-03-01").json()["inventory"]["sources"], [])

    def test_mixed_batch_raw_and_integrity(self):
        result = self.ingest(rows=[self.row(), self.row("bad", 0)])
        self.assertEqual(result.status_code, 201)
        batch_id = result.json()["batch_id"]
        self.assertEqual(result.json()["valid_rows"], 1)
        self.assertEqual(result.json()["rejections"][0]["code"], "invalid_count")
        self.assertEqual(len(self.client.get(f"/batches/{batch_id}/raw").json()["rows"]), 2)
        self.assertTrue(self.client.get(f"/batches/{batch_id}/integrity").json()["valid"])

    def test_batch_replay_is_same_identity(self):
        first = self.ingest().json()
        second = self.ingest().json()
        self.assertEqual(first["batch_id"], second["batch_id"])
        self.assertTrue(second["replay"])

    def test_batch_key_conflict_has_409(self):
        self.ingest()
        result = self.ingest(rows=[self.row(count=8)])
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()["error"]["code"], "conflict")

    def test_staging_does_not_publish(self):
        self.ingest()
        candidate = self.client.post("/snapshots/stage", json={"venue_id": "venue", "local_day": "2026-03-01"}).json()
        self.assertEqual(candidate["status"], "staged")
        self.assertEqual(self.client.get("/venues/venue/days/2026-03-01/report").status_code, 404)
        self.assertEqual(self.client.get(f"/snapshots/{candidate['snapshot_id']}/verify").json()["events"], 1)

    def test_published_report_and_csv(self):
        self.ingest()
        snapshot = self.publish()
        report = self.client.get("/venues/venue/days/2026-03-01/report")
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.json()["total_visitors"], 5)
        csv_result = self.client.get("/venues/venue/days/2026-03-01/export.csv")
        self.assertEqual(csv_result.status_code, 200)
        self.assertIn(snapshot, csv_result.text)

    def test_event_filter_and_export(self):
        self.ingest(rows=[self.row("v1", 5), self.row("v2", 7)])
        self.publish()
        result = self.client.get("/venues/venue/days/2026-03-01/events?kind=visit&limit=1")
        self.assertEqual(result.json()["total"], 2)
        self.assertEqual(len(result.json()["events"]), 1)
        self.assertIn("v2", self.client.get("/venues/venue/days/2026-03-01/events.csv").text)

    def test_source_quality_and_contract_resolution(self):
        quality = self.client.get("/venues/venue/sources/quality").json()
        self.assertEqual(quality["ready_sources"], 1)
        timeline = self.client.get("/sources/counter/contracts").json()
        self.assertEqual(timeline["revisions"][0]["contract_id"], "v1")
        resolved = self.client.get("/sources/counter/contract-at", params={"event_at": "2026-03-01T10:00:00+08:00"}).json()
        self.assertEqual(resolved["contract_id"], "v1")

    def test_provenance_and_audit(self):
        batch = self.ingest().json()
        snapshot = self.publish()
        result = self.client.get("/sources/counter/events/v1/provenance").json()
        self.assertEqual(result["batch"]["batch_id"], batch["batch_id"])
        self.assertEqual(result["snapshots"][0]["snapshot_id"], snapshot)
        trail = self.client.get("/audits?action=snapshot.published").json()
        self.assertEqual(trail["audits"][0]["subject"], snapshot)

    def test_coverage_and_experience(self):
        self.ingest()
        self.publish()
        coverage = self.client.get("/venues/venue/days/2026-03-01/coverage").json()
        self.assertEqual(coverage["pending_events"], 0)
        experience = self.client.get("/venues/venue/days/2026-03-01/exhibits/robot/experience").json()
        self.assertEqual(experience["visitors"], 5)
        self.assertIsNone(experience["rating_average"])

    def test_recovery_diagnostics(self):
        self.ingest()
        candidate = self.client.post("/snapshots/stage", json={"venue_id": "venue", "local_day": "2026-03-01"}).json()
        snapshot = candidate["snapshot_id"]
        self.assertTrue(self.client.get(f"/snapshots/{snapshot}/diagnostics").json()["publishable"])
        self.assertEqual(self.client.post("/venues/venue/days/2026-03-01/abandon-stale").json()["abandoned_ids"], [])

    def test_partition_quality(self):
        self.ingest()
        result = self.client.get("/venues/venue/days/2026-03-01/quality")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["pending_events"], 1)

    def test_venue_integrity(self):
        self.ingest()
        self.publish()
        self.assertTrue(self.client.get("/venues/venue/integrity").json()["valid"])


class ApiMigrationTests(unittest.TestCase):
    """通过 HTTP 契约验证生效日迁移与身份映射。"""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.client = TestClient(create_app(str(Path(self.folder.name) / "api.db")))
        for venue_id, timezone in [("showroom", "Asia/Shanghai"), ("museum", "America/New_York")]:
            self.assertEqual(self.client.post("/venues", json={
                "venue_id": venue_id, "name": venue_id, "timezone": timezone}).status_code, 201)
        self.assertEqual(self.client.post("/exhibits", json={
            "exhibit_id": "robot", "venue_id": "showroom", "name": "Robot",
            "maker": "Maker", "launched_on": "2026-01-01"}).status_code, 201)
        for source, venue in [("show-visits", "showroom"), ("museum-visits", "museum")]:
            self.client.post("/sources", json={"source_id": source, "venue_id": venue, "kind": "visit"})
            self.client.post("/contracts", json={"contract_id": source + "-v1", "source_id": source,
                                                 "fields": ["event_id", "exhibit_id", "event_at", "count"],
                                                 "effective_from": "2026-01-01T00:00:00+00:00"})

    def migrate(self, **overrides):
        payload = {"migration_id": "m1", "exhibit_id": "robot", "to_venue_id": "museum",
                   "effective_on": "2026-03-10", "request_key": "migration-1"}
        payload.update(overrides)
        return self.client.post("/exhibits/robot/migrations", json=payload)

    def test_migration_lifecycle(self):
        created = self.migrate()
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["from_venue_id"], "showroom")
        self.assertEqual(created.json()["to_venue_id"], "museum")
        replay = self.migrate()
        self.assertTrue(replay.json()["replay"])
        placements = self.client.get("/exhibits/robot/placements").json()["placements"]
        self.assertEqual(len(placements), 2)
        self.assertEqual(self.client.get("/exhibits/robot/conflicts").json()["conflict_count"], 0)
        retracted = self.client.post("/migrations/m1/retract", json={"retracted_on": "2026-03-15"})
        self.assertEqual(retracted.status_code, 200, retracted.text)
        self.assertEqual(retracted.json()["status"], "retracted")
        self.assertEqual(self.client.post("/migrations/m1/retract",
                                          json={"retracted_on": "2026-03-16"}).status_code, 409)

    def test_migration_conflicts_have_error_envelope(self):
        self.migrate()
        conflict = self.migrate(migration_id="m2", request_key="another")
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"], "conflict")
        self.assertIn("chronological", conflict.json()["error"]["message"])
        self.assertEqual(self.client.get("/exhibits/robot/placements").json()["placements"][1]["venue_id"],
                         "museum")

    def test_alias_round_trip(self):
        self.migrate()
        registered = self.client.post("/exhibits/robot/aliases", json={
            "alias_id": "R-001", "exhibit_id": "robot", "effective_from": "2026-03-10"})
        self.assertEqual(registered.status_code, 201, registered.text)
        resolved = self.client.get("/exhibits/R-001/resolve",
                                   params={"event_at": "2026-03-12T12:00:00-04:00"}).json()
        self.assertEqual(resolved["exhibit_id"], "robot")
        self.assertEqual(resolved["venue_id"], "museum")
        self.assertTrue(resolved["alias"])
        retired = self.client.post("/exhibit-aliases/R-001/retire", json={"retired_on": "2026-03-20"})
        self.assertEqual(retired.status_code, 200)
        self.assertEqual(retired.json()["effective_to"], "2026-03-19")

    def test_ingest_after_migration_uses_new_venue_partition(self):
        self.migrate()
        accepted = self.client.post("/batches", json={
            "source_id": "museum-visits", "contract_id": "museum-visits-v1", "request_key": "ny",
            "events": [{"event_id": "e1", "exhibit_id": "robot",
                        "event_at": "2026-03-11T23:00:00-04:00", "count": 4}]})
        self.assertEqual(accepted.json()["valid_rows"], 1)
        inventory = self.client.get("/partitions/museum/2026-03-11").json()["inventory"]
        self.assertEqual(inventory["sources"][0]["events"], 1)
        stale = self.client.post("/batches", json={
            "source_id": "show-visits", "contract_id": "show-visits-v1", "request_key": "old",
            "events": [{"event_id": "e2", "exhibit_id": "robot",
                        "event_at": "2026-03-11T23:00:00+08:00", "count": 4}]})
        self.assertEqual(stale.json()["rejections"][0]["code"], "outside_source_venue")

    def test_migration_listing_and_resolve_before_placement(self):
        self.assertEqual(self.client.get("/migrations").json()["migrations"], [])
        self.migrate()
        self.assertEqual(len(self.client.get("/migrations").json()["migrations"]), 1)
        before = self.client.get("/exhibits/robot/resolve",
                                 params={"event_at": "2026-02-01T12:00:00+08:00"}).json()
        self.assertEqual(before["venue_id"], "showroom")
        self.assertEqual(self.client.get("/exhibits/robot/migrations").json()["migrations"][0]["status"], "active")
