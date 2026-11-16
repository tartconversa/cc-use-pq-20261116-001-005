"""按生效时刻解析展品所在场馆、迁移区间与身份别名。

模型约定：

* ``exhibit_placements`` 保存 UTC 生效区间 ``[effective_from, effective_to)``，
  ``effective_to`` 为空表示仍在展。
* 迁移以**目标场馆的本地日**命名：``effective_on`` 当天零点（目标馆时区）为
  切换时刻 ``cutover_at``。切换前后每个事件时刻只属于一个场馆，因此不会因
  时区差异把同一即时事件算进两馆。
* 事件入湖时按其时刻解析场馆，并用**该场馆时区**计算 ``local_day``。历史
  已入湖行的 ``venue_id``/``local_day`` 不再改写。
* 展品身份保持全局唯一。别名把各馆沿用的本地编号映射到同一展品，别名只在
  生效窗口内解析；窗口外该编号不被追认为新馆数据，历史查询保持原始归属。
"""

from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo

from exhibit_lake.catalog.common import iso_day, iso_time, now, require_id
from exhibit_lake.catalog.store import Store
from exhibit_lake.catalog.timeutil import day_start_utc, local_day, next_day
from exhibit_lake.errors import Conflict, InvalidInput, NotFound


class Placements:
    def __init__(self, store: Store):
        self.store = store

    # ---------- 解析 ----------

    def covering(self, conn, exhibit_id: str, instant: str) -> dict | None:
        return Store.one(conn, "SELECT * FROM exhibit_placements WHERE exhibit_id=? "
                               "AND effective_from<=? AND (effective_to IS NULL OR effective_to>?) "
                               "ORDER BY effective_from DESC LIMIT 1", (exhibit_id, instant, instant))

    @staticmethod
    def venue(conn, venue_id: str) -> dict:
        row = Store.one(conn, "SELECT * FROM venues WHERE venue_id=?", (venue_id,))
        if not row:
            raise NotFound("venue not found")
        return row

    def venue_at(self, conn, exhibit_id: str, instant: str) -> str | None:
        row = self.covering(conn, exhibit_id, instant)
        return row["venue_id"] if row else None

    def identity_at(self, conn, requested_id: str, instant: str) -> dict | None:
        exhibit = Store.one(conn, "SELECT * FROM exhibits WHERE exhibit_id=?", (requested_id,))
        if exhibit:
            return exhibit
        aliases = Store.many(conn, "SELECT * FROM exhibit_aliases WHERE alias_id=? "
                                   "ORDER BY effective_from_day,created_at", (requested_id,))
        if not aliases:
            return None
        # 别名窗口按该时刻展品所在场馆的本地日解释；尚未落位时退回登记馆。
        target_id = aliases[0]["exhibit_id"]
        placement = self.covering(conn, target_id, instant)
        venue_id = placement["venue_id"] if placement else self._primary_venue(conn, target_id)
        day = local_day(instant, self._zone(conn, venue_id))
        for alias in aliases:
            if alias["exhibit_id"] != target_id:
                continue
            if alias["effective_from_day"] <= day and (
                    alias["effective_to_day"] is None or day <= alias["effective_to_day"]):
                target = Store.one(conn, "SELECT * FROM exhibits WHERE exhibit_id=?", (target_id,))
                return {**target, "alias_of": target_id, "alias_id": requested_id,
                        "alias_window": {"effective_from": alias["effective_from_day"],
                                         "effective_to": alias["effective_to_day"]}}
        return None

    def resolve_event(self, conn, requested_id: str, event_at: str) -> dict | None:
        """返回事件应归属的展品与场馆；无身份时返回 None。"""
        identity = self.identity_at(conn, requested_id, event_at)
        if not identity:
            return None
        placement = self.covering(conn, identity["exhibit_id"], event_at)
        venue_id = placement["venue_id"] if placement else self._primary_venue(conn, identity["exhibit_id"])
        return {**identity, "placement": placement, "venue_id": venue_id,
                "local_day": local_day(event_at, self._zone(conn, venue_id)),
                "placed": placement is not None}

    def resolve(self, exhibit_id: str, event_at: str) -> dict:
        require_id(exhibit_id, "exhibit_id")
        stamp = iso_time(event_at, "event_at")
        with self.store.transaction(write=False) as conn:
            evidence = self.resolve_event(conn, exhibit_id, stamp)
            if not evidence:
                raise NotFound("exhibit identity not found")
            return {"requested_exhibit_id": exhibit_id, "exhibit_id": evidence["exhibit_id"],
                    "alias": "alias_of" in evidence, "venue_id": evidence["venue_id"],
                    "placed": evidence["placed"], "local_day": evidence["local_day"],
                    "launched_on": evidence["launched_on"], "withdrawn_on": evidence["withdrawn_on"]}

    def venue_on_local_day(self, conn, exhibit_id: str, venue_id: str, day: str) -> dict | None:
        """展品在该场馆的某本地日是否在场（按该馆时区换算的 UTC 窗口）。"""
        venue = Store.one(conn, "SELECT timezone FROM venues WHERE venue_id=?", (venue_id,))
        if not venue:
            return None
        start, end = day_start_utc(day, venue["timezone"]), day_start_utc(next_day(day), venue["timezone"])
        return Store.one(conn, "SELECT * FROM exhibit_placements WHERE exhibit_id=? AND venue_id=? "
                               "AND effective_from<? AND (effective_to IS NULL OR effective_to>?) "
                               "ORDER BY effective_from DESC LIMIT 1", (exhibit_id, venue_id, end, start))

    def identity_on_local_day(self, conn, requested_id: str, venue_id: str, day: str) -> dict | None:
        """在给定场馆的某本地日解析请求身份（含有效窗口内的别名）。"""
        venue = Store.one(conn, "SELECT timezone FROM venues WHERE venue_id=?", (venue_id,))
        if not venue:
            raise NotFound("venue not found")
        return self.identity_at(conn, requested_id, day_start_utc(day, venue["timezone"]))

    def available_on_local_day(self, conn, venue_id: str, day: str) -> list[dict]:
        """该场馆某本地日可供上报的展品及其驻留区间。"""
        venue = Store.one(conn, "SELECT timezone FROM venues WHERE venue_id=?", (venue_id,))
        if not venue:
            raise NotFound("venue not found")
        start, end = day_start_utc(day, venue["timezone"]), day_start_utc(next_day(day), venue["timezone"])
        return Store.many(conn, "SELECT e.*,p.placement_id,p.effective_from,p.effective_to FROM exhibit_placements p "
                                "JOIN exhibits e ON e.exhibit_id=p.exhibit_id WHERE p.venue_id=? "
                                "AND p.effective_from<? AND (p.effective_to IS NULL OR p.effective_to>?) "
                                "ORDER BY e.exhibit_id", (venue_id, end, start))

    # ---------- 登记辅助（调用方负责事务） ----------

    def open_placement(self, conn, exhibit_id: str, venue_id: str,
                       start_instant: str, end_instant: str | None = None) -> dict:
        overlap = Store.one(conn, "SELECT * FROM exhibit_placements WHERE exhibit_id=? "
                                  "AND effective_from<? AND (effective_to IS NULL OR effective_to>?) "
                                  "ORDER BY effective_from LIMIT 1",
                            (exhibit_id, end_instant or "9999-12-31", start_instant))
        if overlap:
            raise Conflict("exhibit placement overlaps an existing interval",
                           details={"placement_id": overlap["placement_id"]})
        suffix = 1 + len(Store.many(conn, "SELECT placement_id FROM exhibit_placements WHERE exhibit_id=?",
                                    (exhibit_id,)))
        placement_id = f"{exhibit_id}@{venue_id}#{suffix}"
        conn.execute("INSERT INTO exhibit_placements VALUES(?,?,?,?,?,?,?)",
                     (placement_id, exhibit_id, venue_id, start_instant, end_instant, None, now()))
        return Store.one(conn, "SELECT * FROM exhibit_placements WHERE placement_id=?", (placement_id,))

    def close_from(self, conn, exhibit_id: str, instant: str) -> list[str]:
        """把仍在进行且开始于给定时刻之前的驻留截断到 ``instant``。"""
        closed = []
        for row in Store.many(conn, "SELECT * FROM exhibit_placements WHERE exhibit_id=? "
                                    "AND effective_from<? AND (effective_to IS NULL OR effective_to>?)",
                              (exhibit_id, instant, instant)):
            conn.execute("UPDATE exhibit_placements SET effective_to=? WHERE placement_id=?",
                         (instant, row["placement_id"]))
            closed.append(row["placement_id"])
        return closed

    def has_future(self, conn, exhibit_id: str, instant: str) -> dict | None:
        return Store.one(conn, "SELECT * FROM exhibit_placements WHERE exhibit_id=? AND effective_from>=? "
                               "ORDER BY effective_from LIMIT 1", (exhibit_id, instant))

    def current_placement(self, conn, exhibit_id: str) -> dict | None:
        """当前仍在进行的驻留，用于撤展时按所在场馆时区截断。"""
        return Store.one(conn, "SELECT p.*,v.timezone FROM exhibit_placements p "
                               "JOIN venues v ON v.venue_id=p.venue_id WHERE p.exhibit_id=? "
                               "AND p.effective_to IS NULL ORDER BY p.effective_from DESC LIMIT 1",
                         (exhibit_id,))

    # ---------- 读取 ----------

    def placements(self, exhibit_id: str) -> list[dict]:
        with self.store.transaction(write=False) as conn:
            self._exhibit(conn, exhibit_id)
            return Store.many(conn, "SELECT * FROM exhibit_placements WHERE exhibit_id=? "
                                    "ORDER BY effective_from", (exhibit_id,))

    def aliases(self, exhibit_id: str) -> list[dict]:
        with self.store.transaction(write=False) as conn:
            self._exhibit(conn, exhibit_id)
            return Store.many(conn, "SELECT alias_id,exhibit_id,effective_from_day,effective_to_day,created_at "
                                    "FROM exhibit_aliases WHERE exhibit_id=? ORDER BY effective_from_day",
                              (exhibit_id,))

    def migrations(self, exhibit_id: str | None = None) -> list[dict]:
        with self.store.transaction(write=False) as conn:
            if exhibit_id:
                self._exhibit(conn, exhibit_id)
                rows = Store.many(conn, "SELECT * FROM exhibit_migrations WHERE exhibit_id=? "
                                        "ORDER BY cutover_at,migration_id", (exhibit_id,))
            else:
                rows = Store.many(conn, "SELECT * FROM exhibit_migrations ORDER BY cutover_at,migration_id")
            return [self._public(row) for row in rows]

    # ---------- 别名 ----------

    def register_alias(self, alias_id: str, exhibit_id: str, effective_from: str | None = None,
                       effective_to: str | None = None, request_key: str | None = None) -> dict:
        require_id(alias_id, "alias_id")
        if request_key is not None:
            require_id(request_key, "request_key")
        with self.store.transaction() as conn:
            self._exhibit(conn, exhibit_id)
            replay = Store.one(conn, "SELECT * FROM exhibit_aliases WHERE alias_id=? AND request_key=?",
                               (alias_id, request_key)) if request_key else None
            if replay:
                if (replay["exhibit_id"], replay["effective_from_day"], replay["effective_to_day"]) != (
                        exhibit_id, effective_from or replay["effective_from_day"], effective_to):
                    raise Conflict("request key already used for a different alias")
                return {**self._alias_public(replay), "replay": True}
            # 别名只允许指向新身份，不能覆盖已存在的展品 ID。
            if Store.one(conn, "SELECT alias_id FROM exhibit_aliases WHERE alias_id=? "
                               "UNION SELECT exhibit_id FROM exhibits WHERE exhibit_id=?", (alias_id, alias_id)):
                raise Conflict("alias collides with an existing exhibit identity")
            start = effective_from or self._first_day(conn, exhibit_id)
            iso_day(start, "effective_from")
            if effective_to is not None:
                iso_day(effective_to, "effective_to")
                if effective_to < start:
                    raise InvalidInput("alias interval is empty")
            for row in Store.many(conn, "SELECT * FROM exhibit_aliases WHERE alias_id=?", (alias_id,)):
                if start <= (row["effective_to_day"] or "9999-12-31") and row["effective_from_day"] <= (effective_to or "9999-12-31"):
                    raise Conflict("alias effective intervals overlap",
                                   details={"effective_from": row["effective_from_day"],
                                            "effective_to": row["effective_to_day"]})
            conn.execute("INSERT INTO exhibit_aliases VALUES(?,?,?,?,?,?)",
                         (alias_id, exhibit_id, start, effective_to, request_key, now()))
            Store.audit(conn, "exhibit.alias_registered", exhibit_id,
                        {"alias_id": alias_id, "effective_from": start, "effective_to": effective_to},
                        now())
            return {"alias_id": alias_id, "exhibit_id": exhibit_id, "effective_from": start,
                    "effective_to": effective_to, "replay": False}

    def retire_alias(self, alias_id: str, day: str) -> dict:
        iso_day(day, "retired_on")
        with self.store.transaction() as conn:
            alias = Store.one(conn, "SELECT * FROM exhibit_aliases WHERE alias_id=? AND effective_to_day IS NULL "
                                    "ORDER BY effective_from_day DESC LIMIT 1", (alias_id,))
            if not alias:
                raise NotFound("active alias not found")
            if day <= alias["effective_from_day"]:
                raise InvalidInput("alias retirement must be after its effective start")
            closed = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
            recorded = Store.one(conn, "SELECT COUNT(*) AS n FROM accepted_events WHERE requested_exhibit_id=? "
                                       "AND exhibit_id=? AND local_day<=?",
                                 (alias_id, alias["exhibit_id"], closed))["n"]
            conn.execute("UPDATE exhibit_aliases SET effective_to_day=? WHERE alias_id=? AND effective_from_day=?",
                         (closed, alias_id, alias["effective_from_day"]))
            Store.audit(conn, "exhibit.alias_retired", alias["exhibit_id"],
                        {"alias_id": alias_id, "effective_to": closed, "recorded_events": recorded}, now())
            return {"alias_id": alias_id, "exhibit_id": alias["exhibit_id"],
                    "effective_to": closed, "recorded_events": recorded}

    # ---------- 迁移 ----------

    def migrate(self, migration_id: str, exhibit_id: str, to_venue_id: str, effective_on: str,
                request_key: str | None = None) -> dict:
        require_id(migration_id, "migration_id")
        iso_day(effective_on, "effective_on")
        if request_key is not None:
            require_id(request_key, "request_key")
        with self.store.transaction() as conn:
            exhibit = self._exhibit(conn, exhibit_id)
            if effective_on < exhibit["launched_on"]:
                raise InvalidInput("migration precedes launch")
            if exhibit["withdrawn_on"] and effective_on > exhibit["withdrawn_on"]:
                raise InvalidInput("migration is after exhibit withdrawal")
            if request_key is not None:
                replay = Store.one(conn, "SELECT * FROM exhibit_migrations WHERE exhibit_id=? AND request_key=?",
                                   (exhibit_id, request_key))
                if replay:
                    if (replay["migration_id"], replay["to_venue_id"], replay["effective_on"]) != (
                            migration_id, to_venue_id, effective_on):
                        raise Conflict("request key already used for a different migration")
                    return self._describe(conn, replay["migration_id"], replay=True)
            if Store.one(conn, "SELECT migration_id FROM exhibit_migrations WHERE migration_id=?", (migration_id,)):
                raise Conflict("migration already exists")
            target = Store.one(conn, "SELECT * FROM venues WHERE venue_id=?", (to_venue_id,))
            if not target:
                raise NotFound("venue not found")
            cutover = day_start_utc(effective_on, target["timezone"])
            latest = Store.one(conn, "SELECT * FROM exhibit_migrations WHERE exhibit_id=? AND status='active' "
                                     "ORDER BY cutover_at DESC LIMIT 1", (exhibit_id,))
            if latest and cutover <= latest["cutover_at"]:
                raise Conflict("migration cutovers must be chronological",
                               details={"latest_migration_id": latest["migration_id"],
                                        "latest_effective_on": latest["effective_on"]})
            source = self.covering(conn, exhibit_id, cutover)
            if not source:
                raise Conflict("no placement covers the migration cutover",
                               details={"cutover_at": cutover})
            if source["venue_id"] == to_venue_id:
                raise Conflict("exhibit already placed at target venue on that day",
                               details={"venue_id": to_venue_id})
            if source["effective_from"] >= cutover:
                raise Conflict("migration day would leave no source interval",
                               details={"placement_id": source["placement_id"]})
            if source["effective_to"] is not None and source["effective_to"] <= cutover:
                raise Conflict("source placement ended before the migration cutover")
            overlapping = Store.many(conn, "SELECT placement_id,venue_id FROM exhibit_placements "
                                           "WHERE exhibit_id=? AND placement_id<>? AND effective_from<? "
                                           "AND (effective_to IS NULL OR effective_to>?)",
                                     (exhibit_id, source["placement_id"], source["effective_to"] or "9999-12-31",
                                      cutover))
            if overlapping:
                raise Conflict("migration window overlaps another placement",
                               details={"placements": overlapping})
            # 已发布的事件不能被重新解释成新馆数据：若来源馆在其后已有发布，拒绝迁移。
            published = Store.many(conn, "SELECT e.source_id,e.event_id,e.local_day,e.event_at,"
                                         "GROUP_CONCAT(DISTINCT m.snapshot_id) AS snapshots "
                                         "FROM accepted_events e LEFT JOIN snapshot_members m "
                                         "ON m.source_id=e.source_id AND m.event_id=e.event_id "
                                         "WHERE e.exhibit_id=? AND e.venue_id=? AND e.event_at>=? "
                                         "AND e.published_at IS NOT NULL "
                                         "GROUP BY e.source_id,e.event_id ORDER BY e.local_day,e.event_id",
                                   (exhibit_id, source["venue_id"], cutover))
            if published:
                raise Conflict("published snapshots already contain this exhibit after the migration day",
                               details={"published_events": published[:20],
                                        "published_event_count": len(published)})
            suffix = 1 + len(Store.many(conn, "SELECT placement_id FROM exhibit_placements WHERE exhibit_id=?",
                                        (exhibit_id,)))
            target_placement_id = f"{exhibit_id}@{to_venue_id}#{suffix}"
            conn.execute("UPDATE exhibit_placements SET effective_to=? WHERE placement_id=?",
                         (cutover, source["placement_id"]))
            conn.execute("INSERT INTO exhibit_placements VALUES(?,?,?,?,?,?,?)",
                         (target_placement_id, exhibit_id, to_venue_id, cutover,
                          source["effective_to"], migration_id, now()))
            conn.execute("INSERT INTO exhibit_migrations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                         (migration_id, exhibit_id, source["venue_id"], to_venue_id, effective_on, cutover,
                          source["placement_id"], target_placement_id,
                          source["effective_to"], source["effective_to"],
                          "active", request_key, now()))
            Store.audit(conn, "exhibit.migrated", exhibit_id, {
                "migration_id": migration_id, "from_venue_id": source["venue_id"],
                "to_venue_id": to_venue_id, "effective_on": effective_on, "cutover_at": cutover,
                "from_effective_to": cutover, "to_effective_to": source["effective_to"]}, now())
            # 迁移声明前已入湖但未发布的事件：若目标馆来源契约覆盖，则改归属，否则明确列出重放。
            queued = Store.many(conn, "SELECT source_id,event_id,local_day,venue_id,event_at FROM accepted_events "
                                       "WHERE exhibit_id=? AND venue_id=? AND event_at>=? AND published_at IS NULL "
                                       "ORDER BY source_id,event_id",
                                 (exhibit_id, source["venue_id"], cutover))
            requeued = self._move_pending(conn, exhibit_id, queued, target_placement_id)
            outstanding = self._outstanding(conn, exhibit_id, queued)
            result = self._describe(conn, migration_id, replay=False)
            result.update({"pending_events_requeued": requeued,
                           "pending_events_requiring_replay": len(outstanding),
                           "pending_event_keys": outstanding})
            return result

    def retract_migration(self, migration_id: str, day: str) -> dict:
        """撤回迁移：未发布数据按恢复后的布局重新归属，已发布快照保持不变。"""
        iso_day(day, "retracted_on")
        with self.store.transaction() as conn:
            migration = Store.one(conn, "SELECT * FROM exhibit_migrations WHERE migration_id=?", (migration_id,))
            if not migration:
                raise NotFound("migration not found")
            if migration["status"] != "active":
                raise Conflict("migration already retracted",
                               details={"retracted_on": migration["retracted_on"]})
            if day < migration["effective_on"]:
                raise InvalidInput("retraction precedes migration")
            later = Store.one(conn, "SELECT * FROM exhibit_migrations WHERE exhibit_id=? AND status='active' "
                                    "AND cutover_at>? ORDER BY cutover_at LIMIT 1",
                              (migration["exhibit_id"], migration["cutover_at"]))
            if later:
                raise Conflict("a later migration depends on this one",
                               details={"migration_id": later["migration_id"]})
            target = Store.one(conn, "SELECT * FROM exhibit_placements WHERE placement_id=?",
                               (migration["to_placement_id"],))
            if not target or target["migration_id"] != migration_id:
                raise Conflict("migration placement was changed; reconcile before retracting")
            published = Store.many(conn, "SELECT DISTINCT s.snapshot_id,s.venue_id,s.local_day FROM snapshots s "
                                        "WHERE s.status='published' AND s.venue_id IN (?,?) "
                                        "AND s.local_day>=? AND EXISTS(SELECT 1 FROM accepted_events e "
                                        "WHERE e.exhibit_id=? AND e.local_day=s.local_day AND e.venue_id=s.venue_id "
                                        "AND e.event_at>=?) ORDER BY s.local_day,s.snapshot_id",
                                  (migration["from_venue_id"], migration["to_venue_id"],
                                   migration["effective_on"], migration["exhibit_id"],
                                   migration["cutover_at"]))
            pending = Store.many(conn, "SELECT source_id,event_id,local_day,venue_id,event_at FROM accepted_events "
                                       "WHERE exhibit_id=? AND event_at>=? AND published_at IS NULL "
                                       "AND venue_id IN (?,?) ORDER BY source_id,event_id",
                                 (migration["exhibit_id"], migration["cutover_at"],
                                  migration["from_venue_id"], migration["to_venue_id"]))
            conn.execute("UPDATE exhibit_placements SET effective_to=? WHERE placement_id=?",
                         (migration["cutover_at"], migration["to_placement_id"]))
            # 撤回后目标区间归零，不再保留这段空驻留，避免污染后续迁移的重叠判定。
            if Store.one(conn, "SELECT placement_id FROM exhibit_placements WHERE placement_id=? "
                               "AND effective_to<=effective_from", (migration["to_placement_id"],)):
                conn.execute("DELETE FROM exhibit_placements WHERE placement_id=?",
                             (migration["to_placement_id"],))
            conn.execute("UPDATE exhibit_placements SET effective_to=? WHERE placement_id=?",
                         (migration["to_effective_to"], migration["from_placement_id"]))
            resumed = Store.one(conn, "SELECT * FROM exhibit_placements WHERE placement_id=?",
                                (migration["from_placement_id"],))
            if resumed and resumed["effective_to"] is not None and resumed["effective_from"] == resumed["effective_to"]:
                # 来源区间原本在迁移前就已结束：恢复后没有可保留的时段，直接移除。
                conn.execute("DELETE FROM exhibit_placements WHERE placement_id=?",
                             (migration["from_placement_id"],))
            conn.execute("UPDATE exhibit_migrations SET status='retracted',retracted_on=? WHERE migration_id=?",
                         (now(), migration_id))
            # 未发布事件按恢复后的布局重新归属；已发布事件与快照成员保持不变。
            requeued = self._reassign_pending(conn, migration, pending)
            outstanding = self._outstanding(conn, migration["exhibit_id"], pending)
            Store.audit(conn, "exhibit.migration_retracted", migration["exhibit_id"], {
                "migration_id": migration_id, "retracted_on": day,
                "preserved_snapshots": [x["snapshot_id"] for x in published],
                "pending_events_requeued": requeued,
                "pending_events_requiring_replay": len(outstanding)}, now())
            return {**self._describe(conn, migration_id, replay=False), "retracted_on": day,
                    "preserved_snapshots": [{"snapshot_id": x["snapshot_id"], "venue_id": x["venue_id"],
                                             "local_day": x["local_day"]} for x in published],
                    "pending_events_requeued": requeued,
                    "pending_events_requiring_replay": len(outstanding),
                    "pending_event_keys": outstanding}

    def _move_pending(self, conn, exhibit_id: str, events: list[dict], inferred_to: str | None) -> int:
        """把未发布事件按当前驻留重算馆与本地日。

        来源必须已是目标馆且契约覆盖事件时刻；否则保留原行，由调用方重放。
        """
        moved = 0
        for event in events:
            placement = self.covering(conn, exhibit_id, event["event_at"])
            if not placement or placement["venue_id"] == event["venue_id"]:
                continue
            venue = Store.one(conn, "SELECT * FROM venues WHERE venue_id=?", (placement["venue_id"],))
            source = Store.one(conn, "SELECT * FROM sources WHERE source_id=?", (event["source_id"],))
            covers = Store.one(conn, "SELECT contract_id FROM contracts WHERE source_id=? "
                                     "AND effective_from<=? AND (effective_to IS NULL OR effective_to>?)",
                               (event["source_id"], event["event_at"], event["event_at"]))
            if not venue or not source or not covers or source["venue_id"] != placement["venue_id"]:
                continue
            day = local_day(event["event_at"], ZoneInfo(venue["timezone"]))
            conn.execute("UPDATE accepted_events SET venue_id=?,local_day=? "
                         "WHERE source_id=? AND event_id=? AND published_at IS NULL",
                         (placement["venue_id"], day, event["source_id"], event["event_id"]))
            moved += 1
        return moved

    def _outstanding(self, conn, exhibit_id: str, events: list[dict]) -> list[dict]:
        """未发布且仍与当前布局不一致的事件，需要调用方重放或人工处理。"""
        outstanding = []
        for event in events:
            placement = self.covering(conn, exhibit_id, event["event_at"])
            source = Store.one(conn, "SELECT venue_id FROM sources WHERE source_id=?", (event["source_id"],))
            row = Store.one(conn, "SELECT venue_id,local_day FROM accepted_events WHERE source_id=? AND event_id=?",
                            (event["source_id"], event["event_id"]))
            if not row:
                continue
            if not placement or not source or source["venue_id"] != placement["venue_id"]:
                outstanding.append({"source_id": event["source_id"], "event_id": event["event_id"],
                                    "local_day": row["local_day"], "venue_id": row["venue_id"],
                                    "reason": "source_venue_differs_from_placement"})
        return outstanding

    def _reassign_pending(self, conn, migration: dict, pending: list[dict]) -> int:
        return self._move_pending(conn, migration["exhibit_id"], pending, migration["to_placement_id"])

    # ---------- 审计与冲突 ----------

    def conflicts(self, exhibit_id: str) -> dict:
        """复核重叠驻留、同日双馆未发布事件、撤回迁移后的残留。"""
        with self.store.transaction(write=False) as conn:
            self._exhibit(conn, exhibit_id)
            placements = Store.many(conn, "SELECT * FROM exhibit_placements WHERE exhibit_id=? "
                                          "ORDER BY effective_from", (exhibit_id,))
            # 零长度区间（撤回后留下的空窗）不参与重叠判定。
            live = [x for x in placements if x["effective_to"] is None or x["effective_to"] > x["effective_from"]]
            overlaps = []
            for left, right in zip(live, live[1:]):
                if left["effective_to"] is None or left["effective_to"] > right["effective_from"]:
                    overlaps.append({"left": left["placement_id"], "right": right["placement_id"],
                                     "at": max(left["effective_from"], right["effective_from"])})
            duplicates = Store.many(conn, "SELECT local_day,COUNT(DISTINCT venue_id) AS venues FROM accepted_events "
                                          "WHERE exhibit_id=? AND published_at IS NULL GROUP BY local_day "
                                          "HAVING COUNT(DISTINCT venue_id)>1 ORDER BY local_day", (exhibit_id,))
            for row in duplicates:
                row["venue_ids"] = [x["venue_id"] for x in Store.many(
                    conn, "SELECT DISTINCT venue_id FROM accepted_events WHERE exhibit_id=? AND local_day=? "
                          "AND published_at IS NULL ORDER BY venue_id", (exhibit_id, row["local_day"]))]
                # 若两个时区的同一日历日恰好横跨迁移切换点，两馆事件可以同时合理存在；
                # 逐条给出事件时刻可让人复核，而不是悄悄合并。
                row["explained_by_migrations"] = Store.many(
                    conn, "SELECT migration_id,effective_on,cutover_at FROM exhibit_migrations WHERE exhibit_id=? "
                          "AND status='active' ORDER BY cutover_at DESC", (exhibit_id,))
                row["events"] = Store.many(
                    conn, "SELECT source_id,event_id,venue_id,event_at,local_day FROM accepted_events "
                          "WHERE exhibit_id=? AND local_day=? AND published_at IS NULL "
                          "ORDER BY venue_id,event_at,source_id,event_id", (exhibit_id, row["local_day"]))
            stale = []
            for migration in Store.many(conn, "SELECT * FROM exhibit_migrations WHERE exhibit_id=? "
                                              "AND status='retracted'", (exhibit_id,)):
                count = Store.one(conn, "SELECT COUNT(*) AS n FROM accepted_events WHERE exhibit_id=? "
                                        "AND venue_id=? AND event_at>=? AND published_at IS NULL",
                                  (exhibit_id, migration["to_venue_id"], migration["cutover_at"]))["n"]
                if count:
                    stale.append({"migration_id": migration["migration_id"],
                                  "venue_id": migration["to_venue_id"], "pending_events": count})
            return {"exhibit_id": exhibit_id, "placements": placements,
                    "migrations": [self._public(x) for x in Store.many(
                        conn, "SELECT * FROM exhibit_migrations WHERE exhibit_id=? ORDER BY cutover_at",
                        (exhibit_id,))],
                    "overlaps": overlaps, "same_day_multi_venue": duplicates,
                    "retracted_migrations_with_pending": stale,
                    "conflict_count": len(overlaps) + len(duplicates) + len(stale)}

    # ---------- 辅助 ----------

    def _exhibit(self, conn, exhibit_id: str) -> dict:
        exhibit = Store.one(conn, "SELECT * FROM exhibits WHERE exhibit_id=?", (exhibit_id,))
        if not exhibit:
            raise NotFound("exhibit not found")
        return exhibit

    def _first_day(self, conn, exhibit_id: str) -> str:
        row = Store.one(conn, "SELECT MIN(effective_from) AS start FROM exhibit_placements WHERE exhibit_id=?",
                        (exhibit_id,))
        if not row or not row["start"]:
            raise Conflict("exhibit has no placement")
        return local_day(row["start"], self._zone(conn, self._primary_venue(conn, exhibit_id)))

    def _primary_venue(self, conn, exhibit_id: str) -> str:
        row = Store.one(conn, "SELECT venue_id FROM exhibit_placements WHERE exhibit_id=? "
                              "ORDER BY effective_from LIMIT 1", (exhibit_id,))
        if row:
            return row["venue_id"]
        exhibit = self._exhibit(conn, exhibit_id)
        return exhibit["venue_id"]

    @staticmethod
    def _zone(conn, venue_id: str | None) -> ZoneInfo:
        venue = Store.one(conn, "SELECT timezone FROM venues WHERE venue_id=?", (venue_id,)) if venue_id else None
        if not venue:
            raise Conflict("venue not found for placement")
        return ZoneInfo(venue["timezone"])

    @staticmethod
    def _public(row: dict) -> dict:
        return {key: row[key] for key in row if key != "request_key"}

    @staticmethod
    def _alias_public(row: dict) -> dict:
        return {"alias_id": row["alias_id"], "exhibit_id": row["exhibit_id"],
                "effective_from": row["effective_from_day"], "effective_to": row["effective_to_day"]}

    def _describe(self, conn, migration_id: str, *, replay: bool) -> dict:
        migration = Store.one(conn, "SELECT * FROM exhibit_migrations WHERE migration_id=?", (migration_id,))
        if not migration:
            raise NotFound("migration not found")
        return {**self._public(migration), "replay": replay}
