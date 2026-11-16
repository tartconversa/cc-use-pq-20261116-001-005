"""连接登记、入湖、发布与分析流程的应用服务。"""
from __future__ import annotations

from pathlib import Path

from exhibit_lake.catalog.registry import Registry
from exhibit_lake.catalog.placements import Placements
from exhibit_lake.catalog.store import Store
from exhibit_lake.catalog.audit import Audit
from exhibit_lake.catalog.quality import Quality
from exhibit_lake.catalog.contracts import Contracts
from exhibit_lake.ingest.batches import Batches
from exhibit_lake.ingest.preview import Preview
from exhibit_lake.publish.snapshots import Snapshots
from exhibit_lake.publish.recovery import Recovery
from exhibit_lake.query.analytics import Analytics
from exhibit_lake.query.exports import Exports
from exhibit_lake.query.coverage import Coverage
from exhibit_lake.query.experience import Experience
from exhibit_lake.query.provenance import Provenance
from exhibit_lake.query.batch_quality import BatchQuality


class ExhibitLake:
    def __init__(self, database: str | Path):
        self.store = Store(database)
        self.placements = Placements(self.store)
        self.registry = Registry(self.store)
        self.batches = Batches(self.store)
        self.preview = Preview(self.store)
        self.snapshots = Snapshots(self.store)
        self.recovery = Recovery(self.store, self.snapshots)
        self.analytics = Analytics(self.store, self.snapshots)
        self.audit = Audit(self.store)
        self.exports = Exports(self.analytics)
        self.coverage = Coverage(self.store)
        self.experience = Experience(self.store, self.analytics)
        self.quality = Quality(self.store)
        self.contracts = Contracts(self.store)
        self.provenance = Provenance(self.store)
        self.batch_quality = BatchQuality(self.store)

    def register_venue(self, venue_id: str, name: str, timezone: str) -> dict:
        return self.registry.venue(venue_id, name, timezone)

    def register_exhibit(self, exhibit_id: str, venue_id: str, name: str,
                         maker: str, launched_on: str, withdrawn_on: str | None = None) -> dict:
        return self.registry.exhibit(exhibit_id, venue_id, name, maker, launched_on, withdrawn_on)

    def register_source(self, source_id: str, venue_id: str, kind: str) -> dict:
        return self.registry.source(source_id, venue_id, kind)

    def register_contract(self, contract_id: str, source_id: str, fields: list[str],
                          effective_from: str, effective_to: str | None = None) -> dict:
        return self.registry.contract(contract_id, source_id, fields, effective_from, effective_to)

    # ---- 场馆迁移与展品身份映射 ----

    def migrate_exhibit(self, migration_id: str, exhibit_id: str, to_venue_id: str,
                        effective_on: str, request_key: str | None = None) -> dict:
        return self.placements.migrate(migration_id, exhibit_id, to_venue_id, effective_on, request_key)

    def retract_migration(self, migration_id: str, day: str) -> dict:
        return self.placements.retract_migration(migration_id, day)

    def register_exhibit_alias(self, alias_id: str, exhibit_id: str,
                               effective_from: str | None = None,
                               effective_to: str | None = None,
                               request_key: str | None = None) -> dict:
        return self.placements.register_alias(alias_id, exhibit_id, effective_from, effective_to, request_key)

    def retire_exhibit_alias(self, alias_id: str, day: str) -> dict:
        return self.placements.retire_alias(alias_id, day)

    def resolve_exhibit(self, exhibit_id: str, event_at: str) -> dict:
        return self.placements.resolve(exhibit_id, event_at)

    def ingest(self, source_id: str, contract_id: str, request_key: str, events: list[object]) -> dict:
        return self.batches.ingest(source_id, contract_id, request_key, events)

    def publish_day(self, venue_id: str, local_day: str, expected_head: str | None = None) -> dict:
        staged = self.snapshots.stage(venue_id, local_day, expected_head)
        return self.snapshots.publish(staged["snapshot_id"], expected_head)

    def overview(self, venue_id: str, local_day: str) -> dict:
        return {"head": self.snapshots.head(venue_id, local_day),
                "inventory": self.snapshots.inventory(venue_id, local_day)}
