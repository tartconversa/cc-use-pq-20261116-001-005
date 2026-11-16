"""带版本的事件契约与业务时间校验。

场馆归属在**单个事件**层面解析：事件时刻落在哪段驻留区间，就记入哪个场馆，
再用该场馆时区得到本地日。因此迁移当天的跨境事件不会同时出现在两馆分区。
"""
from __future__ import annotations

import hashlib
import json
import math

from exhibit_lake.catalog.registry import iso_time, require_id
from exhibit_lake.errors import InvalidInput


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def fail(code: str, detail: str, **details) -> tuple[None, dict]:
    return None, {"code": code, "detail": detail, **details}


def event(conn, placements, row: object, contract: dict, source: dict) -> tuple[dict | None, dict | None]:
    """``placements`` 为 :class:`Placements`，负责按时刻解析场馆与身份。"""
    if not isinstance(row, dict):
        return fail("not_object", "event must be an object")
    fields = set(json.loads(contract["fields_json"]))
    if set(row) != fields:
        return fail("field_mismatch", "event fields do not match the contract")
    try:
        event_id = require_id(row["event_id"], "event_id")
        exhibit_id = require_id(row["exhibit_id"], "exhibit_id")
        stamp = iso_time(row["event_at"], "event_at")
    except InvalidInput as exc:
        return fail("invalid_identity_or_time", str(exc))
    if stamp < contract["effective_from"] or (contract["effective_to"] and stamp >= contract["effective_to"]):
        return fail("outside_contract", "event time is outside contract interval")
    try:
        identity = placements.resolve_event(conn, exhibit_id, stamp)
    except Exception:  # 迁移布局自检失败按身份问题拒收，不污染批次
        return fail("unknown_exhibit", "exhibit identity could not be resolved")
    if not identity:
        return fail("unknown_exhibit", "exhibit identity is unknown at event time")
    venue_id = identity["venue_id"]
    if not venue_id or not identity["placement"]:
        # 展品已撤展时，撤展后的时刻既没有驻留也没有“迁移”解释，单独回到撤展语义。
        if identity["withdrawn_on"] and identity["local_day"] and identity["local_day"] > identity["withdrawn_on"]:
            return fail("inactive_exhibit", "exhibit was not available on local event day")
        return fail("outside_exhibit_placement",
                    "exhibit was not placed at any venue on the event day")
    if venue_id != source["venue_id"]:
        # 展品已按生效日迁至另一场馆：沿用旧场馆来源的契约不再被接受。
        return fail("outside_source_venue",
                    "exhibit was no longer at this source's venue on the event day",
                    exhibit_venue_id=venue_id, source_venue_id=source["venue_id"])
    venue = placements.venue(conn, venue_id)
    day = identity["local_day"]
    if day < identity["launched_on"] or (identity["withdrawn_on"] and day > identity["withdrawn_on"]):
        return fail("inactive_exhibit", "exhibit was not available on local event day")
    if "channel" in row and (not isinstance(row["channel"], str) or not 1 <= len(row["channel"].strip()) <= 40):
        return fail("invalid_channel", "channel must be a nonempty short string")
    value_num = None
    category = None
    visitor_hash = None
    if source["kind"] == "visit":
        count = row["count"]
        if type(count) is not int or not 1 <= count <= 100000:
            return fail("invalid_count", "visit count must be an integer from 1 to 100000")
        value_num = count
        category = row.get("channel")
    elif source["kind"] == "feedback":
        rating = row["rating"]
        if type(rating) is not int or not 1 <= rating <= 5:
            return fail("invalid_rating", "rating must be an integer from 1 to 5")
        try:
            require_id(row["visitor_id"], "visitor_id")
        except InvalidInput as exc:
            return fail("invalid_visitor", str(exc))
        comment = row.get("comment")
        if comment is not None and (not isinstance(comment, str) or len(comment) > 1000):
            return fail("invalid_comment", "comment must be text of at most 1000 characters")
        value_num = rating
        category = row.get("channel")
        # 访客哈希同时绑定规范展品与上报来源，迁移前后可比较但不泄露原值。
        visitor_hash = hashlib.sha256(
            (identity["exhibit_id"] + ":" + source["venue_id"] + ":" + row["visitor_id"]).encode()).hexdigest()
    elif source["kind"] == "demo":
        duration = row["duration_seconds"]
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 < duration <= 86400:
            return fail("invalid_duration", "demo duration must be finite and within one day")
        if "host_id" in row:
            try:
                require_id(row["host_id"], "host_id")
            except InvalidInput as exc:
                return fail("invalid_host", str(exc))
        value_num = float(duration)
        category = row.get("channel")
    return {"event_id": event_id, "source_id": source["source_id"], "venue_id": venue_id,
            "exhibit_id": identity["exhibit_id"], "requested_exhibit_id": exhibit_id,
            "event_at": stamp, "local_day": day, "kind": source["kind"],
            "value_num": value_num, "category": category, "visitor_hash": visitor_hash,
            "payload_hash": digest(row)}, None
