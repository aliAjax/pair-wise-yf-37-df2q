from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine, has_complete_times, intervals_overlap, track_datetime


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, actor, payload, self._lookup)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        if kind == "movement" and entity["status"] == "recorded":
            self._recompute_movement(entity, actor)
        return self.repository.get_entity(entity_id)

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if entity["kind"] == "movement" and action in ("supply_times", "correct_times"):
            refreshed = self.repository.get_entity(entity_id)
            self._recompute_movement(refreshed, actor)
            updated = refreshed
        return updated

    # --- exposure derivation -------------------------------------------------

    def _create_exposure(self, source, other, case_id, actor, reason):
        data_source = source["data"]
        data_other = other["data"]
        start = max(
            track_datetime(data_source["enter_time"]),
            track_datetime(data_other["enter_time"]),
        )
        end = min(
            track_datetime(data_source["leave_time"], end_of_day=True),
            track_datetime(data_other["leave_time"], end_of_day=True),
        )
        exposure = {
            "case_id": case_id,
            "location": data_source["location"],
            "person_id": data_other["person_id"],
            "source_person_id": data_source["person_id"],
            "source_movement_id": source["id"],
            "other_movement_id": other["id"],
            "overlap_start": start.isoformat(),
            "overlap_end": end.isoformat(),
            "enter_time": data_source["enter_time"],
            "leave_time": data_source["leave_time"],
            "derived_reason": reason,
        }
        entity = self.repository.create_entity(
            str(uuid4()), "exposure", "active", exposure, actor.user_id
        )
        self.audit.record(
            entity["id"], actor, "create", None, "active",
            {"kind": "exposure", "derived_from": [source["id"], other["id"]]},
        )
        return entity

    def _recompute_movement(self, movement, actor):
        """Withdraw exposures derived from this movement, then rebuild from new times."""
        data = movement["data"]
        connected = self.repository.list_entities(kind="exposure")
        for exposure in connected:
            if exposure["status"] != "active":
                continue
            if data.get("case_id") != exposure["data"].get("case_id"):
                continue
            ids = (
                exposure["data"].get("source_movement_id"),
                exposure["data"].get("other_movement_id"),
            )
            if movement["id"] in ids:
                updated = self.repository.update_entity(
                    exposure["id"], exposure["version"], "withdrawn",
                    dict(exposure["data"], withdrawn_reason="movement times corrected"),
                )
                self.audit.record(
                    exposure["id"], actor, "withdraw", "active", "withdrawn",
                    {"reason": "movement times corrected", "movement_id": movement["id"]},
                )
        if movement["status"] != "recorded" or not has_complete_times(data):
            return
        self._derive_exposures_for(movement, actor)

    def _derive_exposures_for(self, movement, actor):
        data = movement["data"]
        case_id = data.get("case_id")
        case = self.repository.get_entity(case_id)
        if not case or case["kind"] != "case":
            return
        case_person = case["data"].get("person_id")
        start = track_datetime(data["enter_time"])
        end = track_datetime(data["leave_time"], end_of_day=True)
        movements = self.repository.list_entities(kind="movement")
        active_exposures = [
            item["data"]
            for item in self.repository.list_entities(kind="exposure")
            if item["status"] == "active"
        ]
        for other in movements:
            if other["id"] == movement["id"] or other["status"] != "recorded":
                continue
            other_data = other["data"]
            if other_data.get("location") != data.get("location"):
                continue
            if not has_complete_times(other_data):
                continue
            other_start = track_datetime(other_data["enter_time"])
            other_end = track_datetime(other_data["leave_time"], end_of_day=True)
            if not intervals_overlap(start, end, other_start, other_end):
                continue
            # Build exposures in both directions when both movements belong to cases.
            pairs = self._exposure_pairs(case, case_person, movement, other)
            for source, target, target_case_id in pairs:
                if self._has_active_exposure(
                    active_exposures, target_case_id, target["id"]
                ):
                    continue
                created = self._create_exposure(
                    source, target, target_case_id, actor, "overlapping location time window"
                )
                active_exposures.append(created["data"])

    def _exposure_pairs(self, case, case_person, movement, other):
        data = movement["data"]
        other_data = other["data"]
        if data.get("person_id") == other_data.get("person_id"):
            return []
        result = []
        if data.get("person_id") == case_person:
            result.append((movement, other, case["id"]))
        other_case = self.repository.get_entity(other_data.get("case_id"))
        if (
            other_case
            and other_case["kind"] == "case"
            and other_case["data"].get("person_id") == other_data.get("person_id")
        ):
            result.append((other, movement, other_case["id"]))
        return result

    @staticmethod
    def _has_active_exposure(active, case_id, movement_id):
        return any(
            item.get("case_id") == case_id
            and movement_id
            in (item.get("source_movement_id"), item.get("other_movement_id"))
            for item in active
        )

    # --- read models ---------------------------------------------------------

    def worklist(self):
        cases = self.repository.list_entities(kind="case")
        contacts = self.repository.list_entities(kind="contact")
        exposures = self.repository.list_entities(kind="exposure")
        movements = self.repository.list_entities(kind="movement")

        contacts_by_case = {}
        for contact in contacts:
            contacts_by_case.setdefault(contact["data"].get("case_id"), []).append(contact)

        case_person_ids = {case["data"].get("person_id") for case in cases}
        symptomatic_person_ids = {
            case["data"].get("person_id")
            for case in cases
            if case["data"].get("symptoms")
        }

        items = []
        total_pending = total_following = total_excluded = total_pending_times = 0

        for case in cases:
            case_id = case["id"]
            case_person = case["data"].get("person_id")
            case_contacts = contacts_by_case.get(case_id, [])
            contact_by_person = {}
            for contact in case_contacts:
                contact_by_person[contact["data"].get("person_id")] = contact

            # Aggregate the active exposures of this case by person so a person
            # with several overlapping tracks appears only once in every bucket.
            overlaps_by_person = {}
            for exposure in exposures:
                if exposure["status"] != "active" or exposure["data"].get("case_id") != case_id:
                    continue
                person_id = exposure["data"].get("person_id")
                if person_id == case_person:
                    continue
                overlaps_by_person.setdefault(person_id, []).append(
                    {
                        "location": exposure["data"].get("location"),
                        "overlap_start": exposure["data"].get("overlap_start"),
                        "overlap_end": exposure["data"].get("overlap_end"),
                        "exposure_id": exposure["id"],
                    }
                )

            pending, following, excluded = [], [], []
            for person_id, overlaps in sorted(overlaps_by_person.items()):
                overlaps.sort(key=lambda item: item["overlap_start"] or "")
                contact = contact_by_person.get(person_id)
                entry = {
                    "person_id": person_id,
                    "contact_id": contact["id"] if contact else None,
                    "contact_status": contact["status"] if contact else None,
                    "overlap_count": len(overlaps),
                    "overlaps": overlaps,
                }
                if person_id in symptomatic_person_ids or person_id in case_person_ids:
                    entry["exclude_reason"] = "本人已出现症状/已是病例"
                    excluded.append(entry)
                elif contact and contact["status"] == "completed":
                    entry["exclude_reason"] = "观察已完成"
                    excluded.append(entry)
                elif contact and contact["status"] == "following":
                    following.append(entry)
                else:
                    # no contact yet, or a contact still waiting in 'identified'
                    pending.append(entry)

            pending_times = [
                {
                    "id": movement["id"],
                    "location": movement["data"].get("location"),
                    "missing_time_reason": movement["data"].get("missing_time_reason"),
                }
                for movement in movements
                if movement["data"].get("case_id") == case_id
                and movement["status"] == "pending_time"
            ]
            total_pending += len(pending)
            total_following += len(following)
            total_excluded += len(excluded)
            total_pending_times += len(pending_times)
            items.append(
                {
                    "case_id": case_id,
                    "case_status": case["status"],
                    "person_id": case["data"].get("person_id"),
                    "location": case["data"].get("location"),
                    "onset_date": case["data"].get("onset_date"),
                    "overlap_count": len(overlaps_by_person),
                    "pending": pending,
                    "following": following,
                    "excluded": excluded,
                    "pending_times": pending_times,
                }
            )

        return {
            "items": items,
            "totals": {
                "cases": len(items),
                "overlap_exposures": sum(item["overlap_count"] for item in items),
                "pending_followup": total_pending,
                "following": total_following,
                "excluded": total_excluded,
                "pending_times": total_pending_times,
            },
        }

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
