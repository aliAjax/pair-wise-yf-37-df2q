from datetime import date, datetime, time, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()


def track_datetime(value, end_of_day=False):
    """Parse an ISO 8601 date/datetime track time into a naive datetime."""
    text = str(value or "").strip()
    try:
        if len(text) == 10:
            day = date.fromisoformat(text)
            return datetime.combine(day, time(23, 59, 59) if end_of_day else time(0, 0))
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("invalid time (need ISO 8601): %s" % value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def track_interval(data):
    start = track_datetime(data.get("enter_time"))
    end = track_datetime(data.get("leave_time"), end_of_day=True)
    if start >= end:
        raise ValidationError("leave_time must be after enter_time")
    return start, end


def has_complete_times(data):
    return bool(str(data.get("enter_time") or "").strip()) and bool(
        str(data.get("leave_time") or "").strip()
    )


def intervals_overlap(start_a, end_a, start_b, end_b):
    # Half-open intervals: merely touching at an endpoint is not an overlap.
    return start_a < end_b and start_b < end_a


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


def _find_case(lookup, case_id):
    case = _find_one(lookup, "case", "id", case_id)
    if not case:
        raise ValidationError("case_id does not reference a case: " + str(case_id))
    return case


def _validate_movement(actor, data, lookup):
    case = _find_case(lookup, data.get("case_id"))
    data.setdefault("person_id", case["data"].get("person_id"))
    complete = has_complete_times(data)
    if not complete and not str(data.get("missing_time_reason") or "").strip():
        raise ValidationError(
            "missing_time_reason is required when enter_time/leave_time are absent"
        )
    if complete:
        track_interval(data)
    return None if complete else "pending_time"


def _validate_supply_times(actor, entity, data, lookup):
    track_interval(data)
    return {"missing_time_reason": ""}


def _validate_correct_times(actor, entity, data, lookup):
    track_interval(data)


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


CUSTOM_CREATE = {'case': _validate_case, 'movement': _validate_movement}
CUSTOM_TRANSITIONS = {('case', 'lab_positive'): _validate_lab_positive, ('case', 'mark_probable'): _validate_probable, ('movement', 'supply_times'): _validate_supply_times, ('movement', 'correct_times'): _validate_correct_times}
CUSTOM_INITIAL_STATUS = {'movement': _validate_movement}


class RuleEngine:
    ALIASES = {'cases': 'case', 'contacts': 'contact', 'movements': 'movement', 'exposures': 'exposure'}
    INITIAL_STATUS = {'case': 'reported', 'contact': 'identified', 'movement': 'recorded', 'exposure': 'active'}
    TRANSITIONS = {'case': {'triage': (('reported',), 'investigating'), 'lab_positive': (('investigating',), 'confirmed'), 'mark_probable': (('investigating',), 'probable'), 'recover': (('confirmed', 'probable'), 'recovered'), 'close': (('recovered',), 'closed')}, 'contact': {'begin_followup': (('identified',), 'following'), 'complete_followup': (('following',), 'completed')}, 'movement': {'supply_times': (('pending_time',), 'recorded'), 'correct_times': (('recorded', 'pending_time'), 'recorded')}, 'exposure': {'withdraw': (('active',), 'withdrawn')}}
    CREATE_REQUIRED = {'case': ('person_id', 'onset_date', 'location', 'symptoms'), 'contact': ('case_id', 'person_id', 'exposure_start'), 'movement': ('case_id', 'location')}
    ACTION_REQUIRED = {('case', 'triage'): ('clinician',), ('case', 'lab_positive'): ('lab_id', 'result'), ('case', 'mark_probable'): ('epi_link',), ('case', 'recover'): ('recovered_at',), ('case', 'close'): ('outcome',), ('contact', 'begin_followup'): ('followup_start', 'due_at'), ('contact', 'complete_followup'): ('outcome',), ('movement', 'supply_times'): ('enter_time', 'leave_time'), ('movement', 'correct_times'): ('enter_time', 'leave_time')}
    CREATE_ROLES = {'case': ('admin', 'clinician'), 'contact': ('admin', 'investigator'), 'movement': ('admin', 'investigator')}
    ROLE_ACTIONS = {'triage': ('admin', 'clinician'), 'lab_positive': ('admin', 'lab'), 'mark_probable': ('admin', 'investigator'), 'recover': ('admin', 'clinician'), 'close': ('admin', 'investigator'), 'begin_followup': ('admin', 'investigator'), 'complete_followup': ('admin', 'investigator'), 'supply_times': ('admin', 'investigator'), 'correct_times': ('admin', 'investigator'), ('exposure', 'withdraw'): ()}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind, actor=None, data=None, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        custom = CUSTOM_INITIAL_STATUS.get(kind)
        if custom and data is not None:
            status = custom(actor, data, lookup)
            if status:
                return status
        return self.INITIAL_STATUS[kind]

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
        if kind == "exposure":
            raise PermissionDenied("exposure records are generated by the system only")
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
