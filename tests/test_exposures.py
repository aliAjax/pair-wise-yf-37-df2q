import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ExposureWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")
        self.investigator = Actor("iv-1", "investigator")

    def tearDown(self):
        self.tmp.cleanup()

    def _case(self, person_id="P-1", onset="2026-03-01", location="Cafe-X"):
        return self.service.create(
            self.actor,
            "case",
            {
                "person_id": person_id,
                "onset_date": onset,
                "location": location,
                "symptoms": ["fever"],
            },
        )

    def _movement(self, case_id, location, enter, leave, person_id=None, actor=None):
        payload = {
            "case_id": case_id,
            "location": location,
            "enter_time": enter,
            "leave_time": leave,
        }
        if person_id:
            payload["person_id"] = person_id
        return self.service.create(actor or self.investigator, "movement", payload)

    def _active_exposures(self, case_id=None):
        rows = [
            e
            for e in self.service.list("exposure")
            if e["status"] == "active"
            and (case_id is None or e["data"].get("case_id") == case_id)
        ]
        return rows

    def test_overlapping_same_location_creates_exposure(self):
        case = self._case()
        self._movement(
            case["id"], "Cafe-X", "2026-02-28T09:00", "2026-02-28T11:00"
        )
        contact_move = self._movement(
            case["id"], "Cafe-X", "2026-02-28T10:30", "2026-02-28T12:00",
            person_id="P-2",
        )
        exposures = self._active_exposures(case["id"])
        self.assertEqual(len(exposures), 1)
        exposure = exposures[0]
        self.assertEqual(exposure["data"]["person_id"], contact_move["data"]["person_id"])
        self.assertEqual(exposure["data"]["overlap_start"], "2026-02-28T10:30:00")
        self.assertEqual(exposure["data"]["overlap_end"], "2026-02-28T11:00:00")

    def test_different_location_or_adjacent_window_no_exposure(self):
        case = self._case()
        self._movement(
            case["id"], "Cafe-X", "2026-02-28T09:00", "2026-02-28T10:00"
        )
        # different location
        self._movement(
            case["id"], "Mall-Y", "2026-02-28T09:30", "2026-02-28T10:30",
            person_id="P-2",
        )
        # merely touching at endpoint is not an overlap
        self._movement(
            case["id"], "Cafe-X", "2026-02-28T10:00", "2026-02-28T11:00",
            person_id="P-2",
        )
        self.assertEqual(self._active_exposures(case["id"]), [])

    def test_missing_times_keep_reason_and_can_be_supplied(self):
        case = self._case()
        with self.assertRaises(ValidationError):
            self.service.create(
                self.investigator,
                "movement",
                {"case_id": case["id"], "location": "Cafe-X"},
            )
        pending = self.service.create(
            self.investigator,
            "movement",
            {
                "case_id": case["id"],
                "location": "Cafe-X",
                "missing_time_reason": "患者回忆中，等待监控确认",
            },
        )
        self.assertEqual(pending["status"], "pending_time")
        # incomplete track takes part in no exposure derivation
        self.assertEqual(self._active_exposures(case["id"]), [])
        recorded = self.service.transition(
            self.investigator,
            pending["id"],
            "supply_times",
            {"enter_time": "2026-02-28T09:00", "leave_time": "2026-02-28T11:00"},
        )
        self.assertEqual(recorded["status"], "recorded")
        self.assertEqual(recorded["data"]["missing_time_reason"], "")

        other = self._movement(
            case["id"], "Cafe-X", "2026-02-28T10:00", "2026-02-28T12:00",
            person_id="P-2",
        )
        self.assertEqual(len(self._active_exposures(case["id"])), 1)

    def test_correct_times_withdraws_old_and_recomputes(self):
        case = self._case()
        source = self._movement(
            case["id"], "Cafe-X", "2026-02-28T09:00", "2026-02-28T11:00"
        )
        target = self._movement(
            case["id"], "Cafe-X", "2026-02-28T10:00", "2026-02-28T12:00",
            person_id="P-2",
        )
        self.assertEqual(len(self._active_exposures(case["id"])), 1)
        # correct target away from the source window: old exposure withdrawn
        corrected = self.service.transition(
            self.investigator,
            target["id"],
            "correct_times",
            {"enter_time": "2026-02-28T14:00", "leave_time": "2026-02-28T15:00"},
        )
        self.assertEqual(corrected["status"], "recorded")
        self.assertEqual(self._active_exposures(case["id"]), [])
        withdrawn = [e for e in self.service.list("exposure") if e["status"] == "withdrawn"]
        self.assertEqual(len(withdrawn), 1)
        # correct back into overlap: a new active exposure is derived
        self.service.transition(
            self.investigator,
            target["id"],
            "correct_times",
            {"enter_time": "2026-02-28T10:30", "leave_time": "2026-02-28T11:30"},
        )
        active = self._active_exposures(case["id"])
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["data"]["overlap_start"], "2026-02-28T10:30:00")
        self.assertEqual(active[0]["data"]["overlap_end"], "2026-02-28T11:00:00")

    def test_completed_followup_and_symptomatic_person_leave_pending_list(self):
        case = self._case(person_id="P-1")
        self._movement(case["id"], "Cafe-X", "2026-02-28T09:00", "2026-02-28T12:00")
        p2 = self._movement(
            case["id"], "Cafe-X", "2026-02-28T09:30", "2026-02-28T10:30",
            person_id="P-2",
        )
        p3 = self._movement(
            case["id"], "Cafe-X", "2026-02-28T10:00", "2026-02-28T11:00",
            person_id="P-3",
        )
        # P-2 already completed observation -> excluded from pending
        contact_p2 = self.service.create(
            self.actor,
            "contact",
            {
                "case_id": case["id"],
                "person_id": p2["data"]["person_id"],
                "exposure_start": "2026-02-28",
            },
        )
        self.service.transition(
            self.actor, contact_p2["id"], "begin_followup",
            {"followup_start": "2026-03-01", "due_at": "2026-03-15"},
        )
        self.service.transition(
            self.actor, contact_p2["id"], "complete_followup",
            {"outcome": "no symptoms"},
        )
        # P-3 is now a symptomatic case themselves -> excluded
        self._case(person_id=p3["data"]["person_id"], onset="2026-03-02", location="Home")

        worklist = self.service.worklist()
        item = next(row for row in worklist["items"] if row["case_id"] == case["id"])
        self.assertEqual(item["overlap_count"], 2)
        self.assertEqual(len(item["pending"]), 0)
        excluded_persons = {row["person_id"] for row in item["excluded"]}
        self.assertIn(p2["data"]["person_id"], excluded_persons)
        self.assertIn(p3["data"]["person_id"], excluded_persons)
        self.assertEqual(worklist["totals"]["pending_followup"], 0)

    def test_pending_person_can_become_following(self):
        case = self._case(person_id="P-1")
        self._movement(case["id"], "Cafe-X", "2026-02-28T09:00", "2026-02-28T12:00")
        other = self._movement(
            case["id"], "Cafe-X", "2026-02-28T09:30", "2026-02-28T10:30",
            person_id="P-2",
        )
        worklist = self.service.worklist()
        item = worklist["items"][0]
        self.assertEqual(item["overlap_count"], 1)
        self.assertEqual(len(item["pending"]), 1)
        self.assertEqual(item["pending"][0]["person_id"], other["data"]["person_id"])

        contact = self.service.create(
            self.actor,
            "contact",
            {
                "case_id": case["id"],
                "person_id": other["data"]["person_id"],
                "exposure_start": item["pending"][0]["overlaps"][0]["overlap_start"],
            },
        )
        self.service.transition(
            self.actor, contact["id"], "begin_followup",
            {"followup_start": "2026-03-01", "due_at": "2026-03-15"},
        )
        item = next(row for row in self.service.worklist()["items"] if row["case_id"] == case["id"])
        self.assertEqual(len(item["pending"]), 0)
        self.assertEqual(len(item["following"]), 1)

    def test_exposure_cannot_be_created_manually(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(
                self.actor, "exposure", {"case_id": "x", "person_id": "P-2"}
            )

    def test_movement_rejects_invalid_interval(self):
        case = self._case()
        with self.assertRaises(ValidationError):
            self._movement(
                case["id"], "Cafe-X", "2026-02-28T12:00", "2026-02-28T09:00"
            )


if __name__ == "__main__":
    unittest.main()
