from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()


def _validate_case(actor, data, lookup):
    rows = lookup("case", "person_id", data.get("person_id")) or [] if lookup else []
    for row in rows:
        if row["data"].get("onset_date") == data.get("onset_date"):
            raise ConflictError("duplicate case for person and onset date")
    if not data.get("symptoms"):
        raise ValidationError("symptoms are required")


def _validate_lab_positive(actor, entity, data, lookup):
    if data.get("result", "").lower() not in ("positive", "detected"):
        raise ValidationError("lab result must be positive or detected")
    return {"confirmed_by": actor.user_id}


def _validate_probable(actor, entity, data, lookup):
    if not data.get("epi_link"):
        raise ValidationError("probable case requires an epidemiological link")


def _parse_datetime(value):
    text = str(value)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError("invalid datetime (use ISO 8601): " + text)


def _validate_time_window(enter_at, leave_at):
    start = _parse_datetime(enter_at)
    end = _parse_datetime(leave_at)
    if (start.tzinfo is None) != (end.tzinfo is None):
        raise ValidationError("enter_at and leave_at must use the same timezone style")
    if end <= start:
        raise ValidationError("leave_at must be later than enter_at")
    return start, end


def normalize_location(value):
    return str(value).strip().casefold()


def trajectory_times_complete(data):
    return bool(data.get("enter_at")) and bool(data.get("leave_at"))


def _validate_trajectory(actor, data, lookup):
    case = _find_one(lookup, "case", "id", data.get("case_id"))
    if not case:
        raise ValidationError("case_id must reference an existing case")
    if trajectory_times_complete(data):
        _validate_time_window(data["enter_at"], data["leave_at"])
    elif not data.get("pending_reason"):
        raise ValidationError("pending_reason is required when trajectory times are missing")


def _validate_trajectory_times(actor, entity, data, lookup):
    _validate_time_window(data.get("enter_at"), data.get("leave_at"))


def compute_overlaps(trajectories):
    """Return same-location time overlaps between active trajectories."""

    def as_utc(value):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    visits = []
    for entity in trajectories:
        if entity.get("status") != "active":
            continue
        data = entity["data"]
        if not trajectory_times_complete(data):
            continue
        visits.append(
            {
                "id": entity["id"],
                "case_id": data.get("case_id"),
                "person_id": data.get("person_id"),
                "location": data.get("location"),
                "enter": as_utc(_parse_datetime(data["enter_at"])),
                "leave": as_utc(_parse_datetime(data["leave_at"])),
            }
        )
    groups = {}
    for visit in visits:
        groups.setdefault(normalize_location(visit["location"]), []).append(visit)
    results = []
    for members in groups.values():
        members.sort(key=lambda item: item["enter"])
        for index in range(len(members)):
            for other in range(index + 1, len(members)):
                first, second = members[index], members[other]
                if first["person_id"] and first["person_id"] == second["person_id"]:
                    continue
                if first["enter"] < second["leave"] and second["enter"] < first["leave"]:
                    results.append(
                        {
                            "location": first["location"],
                            "trajectory_ids": sorted([first["id"], second["id"]]),
                            "overlap_start": max(first["enter"], second["enter"]).isoformat(),
                            "overlap_end": min(first["leave"], second["leave"]).isoformat(),
                            "sides": [
                                {
                                    "trajectory_id": first["id"],
                                    "case_id": first["case_id"],
                                    "person_id": first["person_id"],
                                },
                                {
                                    "trajectory_id": second["id"],
                                    "case_id": second["case_id"],
                                    "person_id": second["person_id"],
                                },
                            ],
                        }
                    )
    results.sort(key=lambda item: (item["overlap_start"], item["trajectory_ids"][0]))
    return results


def cluster_cases(cases, max_days=14):
    groups = []
    for case in sorted(cases, key=lambda item: str(item.get("onset_date", ""))):
        placed = False
        for group in groups:
            same_location = group["location"] == case.get("location")
            delta = abs(_date_ordinal(group["onset_date"]) - _date_ordinal(case.get("onset_date")))
            if same_location and delta <= max_days:
                group["members"].append(case.get("id"))
                placed = True
                break
        if not placed:
            groups.append({"location": case.get("location"), "onset_date": case.get("onset_date"), "members": [case.get("id")]})
    return [group for group in groups if len(group["members"]) > 1]


CUSTOM_CREATE = {'case': _validate_case, 'trajectory': _validate_trajectory}
CUSTOM_TRANSITIONS = {('case', 'lab_positive'): _validate_lab_positive, ('case', 'mark_probable'): _validate_probable, ('trajectory', 'supplement_time'): _validate_trajectory_times, ('trajectory', 'correct_time'): _validate_trajectory_times}


class RuleEngine:
    ALIASES = {'cases': 'case', 'contacts': 'contact', 'trajectories': 'trajectory', 'exposures': 'exposure'}
    INITIAL_STATUS = {'case': 'reported', 'contact': 'identified', 'trajectory': 'active', 'exposure': 'active'}
    TRANSITIONS = {'case': {'triage': (('reported',), 'investigating'), 'lab_positive': (('investigating',), 'confirmed'), 'mark_probable': (('investigating',), 'probable'), 'recover': (('confirmed', 'probable'), 'recovered'), 'close': (('recovered',), 'closed')}, 'contact': {'begin_followup': (('identified',), 'following'), 'complete_followup': (('following',), 'completed'), 'withdraw': (('identified',), 'withdrawn'), 'reopen': (('withdrawn',), 'identified')}, 'trajectory': {'supplement_time': (('pending_time',), 'active'), 'correct_time': (('active', 'pending_time'), 'active'), 'invalidate': (('active', 'pending_time'), 'invalid')}}
    CREATE_REQUIRED = {'case': ('person_id', 'onset_date', 'location', 'symptoms'), 'contact': ('case_id', 'person_id', 'exposure_start'), 'trajectory': ('case_id', 'location')}
    ACTION_REQUIRED = {('case', 'triage'): ('clinician',), ('case', 'lab_positive'): ('lab_id', 'result'), ('case', 'mark_probable'): ('epi_link',), ('case', 'recover'): ('recovered_at',), ('case', 'close'): ('outcome',), ('contact', 'begin_followup'): ('followup_start', 'due_at'), ('contact', 'complete_followup'): ('outcome',), ('trajectory', 'supplement_time'): ('enter_at', 'leave_at'), ('trajectory', 'correct_time'): ('enter_at', 'leave_at'), ('trajectory', 'invalidate'): ('reason',)}
    CREATE_ROLES = {'case': ('admin', 'clinician'), 'contact': ('admin', 'investigator'), 'trajectory': ('admin', 'investigator'), 'exposure': ('admin',)}
    ROLE_ACTIONS = {'triage': ('admin', 'clinician'), 'lab_positive': ('admin', 'lab'), 'mark_probable': ('admin', 'investigator'), 'recover': ('admin', 'clinician'), 'close': ('admin', 'investigator'), 'begin_followup': ('admin', 'investigator'), 'complete_followup': ('admin', 'investigator'), 'withdraw': ('admin', 'investigator'), 'reopen': ('admin',), 'supplement_time': ('admin', 'investigator'), 'correct_time': ('admin', 'investigator'), 'invalidate': ('admin', 'investigator')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    def initial_status_for(self, kind, data):
        if self.normalize_kind(kind) == "trajectory" and not trajectory_times_complete(data):
            return "pending_time"
        return self.initial_status(kind)

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
