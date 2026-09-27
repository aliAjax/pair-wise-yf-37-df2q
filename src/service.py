import hashlib
from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, NotFoundError
from .rules import RuleEngine, compute_overlaps

SYSTEM_ACTOR = Actor("system", "admin")


def _exposure_id(trajectory_ids):
    return "expo-" + hashlib.sha1("|".join(sorted(trajectory_ids)).encode("utf-8")).hexdigest()[:16]


def _auto_contact_id(case_id, person_id):
    return "contact-" + hashlib.sha1((str(case_id) + "|" + str(person_id)).encode("utf-8")).hexdigest()[:16]


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
        if kind == "trajectory":
            case = self.repository.get_entity(payload["case_id"])
            payload["person_id"] = payload.get("person_id") or case["data"].get("person_id")
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status_for(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        if kind in ("trajectory", "case"):
            self._reconcile_exposures(actor)
        return entity

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
        if entity["kind"] == "trajectory" and action in ("supplement_time", "correct_time"):
            merged.pop("pending_reason", None)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if entity["kind"] == "trajectory":
            self._reconcile_exposures(actor)
        return updated

    def _reconcile_exposures(self, actor):
        """Recompute exposure relations from active trajectories and sync contacts."""
        trajectories = self.repository.list_entities(kind="trajectory")
        overlaps = compute_overlaps(trajectories)
        existing = {entity["id"]: entity for entity in self.repository.list_entities(kind="exposure")}
        desired = {}
        for overlap in overlaps:
            exposure_id = _exposure_id(overlap["trajectory_ids"])
            desired[exposure_id] = {
                "location": overlap["location"],
                "trajectory_ids": overlap["trajectory_ids"],
                "overlap_start": overlap["overlap_start"],
                "overlap_end": overlap["overlap_end"],
                "sides": overlap["sides"],
            }
        for exposure_id, data in desired.items():
            current = existing.get(exposure_id)
            if current is None:
                self.repository.create_entity(exposure_id, "exposure", "active", data, actor.user_id)
                self.audit.record(exposure_id, actor, "derive", None, "active", {"kind": "exposure"})
            elif current["status"] != "active" or current["data"] != data:
                self.repository.update_entity(exposure_id, None, "active", data)
                self.audit.record(exposure_id, actor, "recalculate", current["status"], "active", {"data": data})
        for exposure_id, current in existing.items():
            if exposure_id not in desired and current["status"] == "active":
                self.repository.update_entity(exposure_id, None, "withdrawn", current["data"])
                self.audit.record(exposure_id, actor, "withdraw", "active", "withdrawn", {"reason": "trajectory changed"})
        self._reconcile_contacts(actor, desired)

    def _reconcile_contacts(self, actor, desired):
        case_people = {
            case["data"].get("person_id")
            for case in self.repository.list_entities(kind="case")
        }
        links = {}
        for data in desired.values():
            first, second = data["sides"]
            for case_side, other_side in ((first, second), (second, first)):
                if not case_side.get("case_id") or not other_side.get("person_id"):
                    continue
                if other_side["person_id"] in case_people:
                    continue
                key = (case_side["case_id"], other_side["person_id"])
                link = links.setdefault(
                    key,
                    {
                        "case_id": case_side["case_id"],
                        "person_id": other_side["person_id"],
                        "exposure_start": data["overlap_start"],
                        "location": data["location"],
                    },
                )
                if data["overlap_start"] < link["exposure_start"]:
                    link["exposure_start"] = data["overlap_start"]
        groups = {}
        for contact in self.repository.list_entities(kind="contact"):
            key = (contact["data"].get("case_id"), contact["data"].get("person_id"))
            groups.setdefault(key, []).append(contact)
        for key, link in links.items():
            members = groups.get(key, [])
            auto = next((item for item in members if item["data"].get("source") == "auto"), None)
            if auto is None and not members:
                contact_id = _auto_contact_id(*key)
                if not self.repository.get_entity(contact_id):
                    payload = dict(link)
                    payload["source"] = "auto"
                    self.repository.create_entity(contact_id, "contact", "identified", payload, actor.user_id)
                    self.audit.record(contact_id, actor, "derive", None, "identified", {"kind": "contact"})
            elif auto is not None and auto["status"] == "withdrawn":
                self.transition(SYSTEM_ACTOR, auto["id"], "reopen", {"reason": "exposure recalculated"})
        for key, members in groups.items():
            auto = next((item for item in members if item["data"].get("source") == "auto"), None)
            if auto is None or auto["status"] != "identified":
                continue
            if key not in links or key[1] in case_people:
                self.transition(SYSTEM_ACTOR, auto["id"], "withdraw", {"reason": "exposure withdrawn or person became a case"})

    def worklist(self):
        trajectories = self.repository.list_entities(kind="trajectory")
        exposures = self.repository.list_entities(kind="exposure")
        contacts = self.repository.list_entities(kind="contact")
        case_people = {
            case["data"].get("person_id")
            for case in self.repository.list_entities(kind="case")
        }
        active_exposures = [item for item in exposures if item["status"] == "active"]
        pending_trajectories = [
            {
                "id": item["id"],
                "case_id": item["data"].get("case_id"),
                "person_id": item["data"].get("person_id"),
                "location": item["data"].get("location"),
                "pending_reason": item["data"].get("pending_reason"),
            }
            for item in trajectories
            if item["status"] == "pending_time"
        ]
        pending_trajectories.sort(key=lambda item: item["id"])
        overlap_people = {
            side.get("person_id")
            for item in active_exposures
            for side in item["data"].get("sides", [])
        } - {None}
        overlap_by_trajectory = {}
        for item in active_exposures:
            sides = item["data"].get("sides", [])
            for side in sides:
                trajectory_id = side.get("trajectory_id")
                if not trajectory_id:
                    continue
                others = {
                    other.get("person_id") for other in sides if other is not side
                } - {None, side.get("person_id")}
                overlap_by_trajectory.setdefault(trajectory_id, set()).update(others)
        overlaps = [
            {
                "id": item["id"],
                "location": item["data"].get("location"),
                "overlap_start": item["data"].get("overlap_start"),
                "overlap_end": item["data"].get("overlap_end"),
                "trajectory_ids": item["data"].get("trajectory_ids"),
                "people": sorted(
                    {side.get("person_id") for side in item["data"].get("sides", [])} - {None}
                ),
            }
            for item in active_exposures
        ]
        overlaps.sort(key=lambda item: (item["overlap_start"] or "", item["id"]))
        pending_followups = [
            {
                "id": item["id"],
                "case_id": item["data"].get("case_id"),
                "person_id": item["data"].get("person_id"),
                "location": item["data"].get("location"),
                "exposure_start": item["data"].get("exposure_start"),
                "status": item["status"],
                "source": item["data"].get("source", "manual"),
            }
            for item in contacts
            if item["status"] in ("identified", "following")
            and item["data"].get("person_id") not in case_people
        ]
        pending_followups.sort(key=lambda item: (item["exposure_start"] or "", item["id"]))
        return {
            "overlap_people_count": len(overlap_people),
            "active_exposure_count": len(active_exposures),
            "pending_trajectory_count": len(pending_trajectories),
            "pending_followup_count": len(pending_followups),
            "pending_trajectories": pending_trajectories,
            "pending_followups": pending_followups,
            "overlaps": overlaps,
            "overlap_people": sorted(overlap_people),
            "overlap_people_by_trajectory": {
                key: len(value) for key, value in sorted(overlap_by_trajectory.items())
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
