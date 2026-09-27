import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine, compute_overlaps
from src.service import DomainService


def make_service():
    tmp = tempfile.TemporaryDirectory()
    repo = SQLiteRepository(Path(tmp.name) / "test.db")
    return tmp, DomainService(repo, RuleEngine())


def case_data(person_id, location="Canteen", onset="2026-03-01"):
    return {
        "person_id": person_id,
        "onset_date": onset,
        "location": location,
        "symptoms": ["fever"],
    }


class TrajectoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.service = make_service()
        self.actor = Actor("investigator-1", "investigator")

    def tearDown(self):
        self.tmp.cleanup()

    def _create_case(self, person_id="P-1"):
        return self.service.create(Actor("admin", "admin"), "case", case_data(person_id))

    def test_overlap_creates_exposure_and_contact(self):
        case = self._create_case()
        self.service.create(
            self.actor,
            "trajectory",
            {
                "case_id": case["id"],
                "location": "Canteen",
                "enter_at": "2026-03-01T12:00",
                "leave_at": "2026-03-01T13:00",
            },
        )
        self.service.create(
            self.actor,
            "trajectory",
            {
                "case_id": case["id"],
                "person_id": "P-2",
                "location": "Canteen",
                "enter_at": "2026-03-01T12:30",
                "leave_at": "2026-03-01T13:30",
            },
        )
        exposures = self.service.list("exposures")
        self.assertEqual(len(exposures), 1)
        self.assertEqual(exposures[0]["status"], "active")
        self.assertEqual(exposures[0]["data"]["location"], "Canteen")
        contacts = self.service.list("contacts")
        self.assertEqual(len(contacts), 1)
        contact = contacts[0]
        self.assertEqual(contact["data"]["person_id"], "P-2")
        self.assertEqual(contact["status"], "identified")
        self.assertEqual(contact["data"]["source"], "auto")
        worklist = self.service.worklist()
        self.assertEqual(worklist["overlap_people_count"], 2)
        self.assertEqual(worklist["pending_followup_count"], 1)
        self.assertEqual(worklist["pending_trajectory_count"], 0)
        self.assertEqual(worklist["pending_followups"][0]["person_id"], "P-2")

    def test_missing_times_require_reason_and_await_supplement(self):
        case = self._create_case()
        with self.assertRaises(ValidationError):
            self.service.create(
                self.actor,
                "trajectory",
                {"case_id": case["id"], "location": "Canteen"},
            )
        pending = self.service.create(
            self.actor,
            "trajectory",
            {
                "case_id": case["id"],
                "person_id": "P-3",
                "location": "Canteen",
                "pending_reason": "monitor footage not yet reviewed",
            },
        )
        self.assertEqual(pending["status"], "pending_time")
        worklist = self.service.worklist()
        self.assertEqual(worklist["pending_trajectory_count"], 1)
        self.assertEqual(worklist["active_exposure_count"], 0)
        active = self.service.transition(
            self.actor,
            pending["id"],
            "supplement_time",
            {"enter_at": "2026-03-01T12:00", "leave_at": "2026-03-01T12:45"},
        )
        self.assertEqual(active["status"], "active")
        self.assertNotIn("pending_reason", active["data"])

    def test_completed_followup_leaves_pending_list(self):
        case = self._create_case()
        self.service.create(
            self.actor,
            "trajectory",
            {
                "case_id": case["id"],
                "location": "Canteen",
                "enter_at": "2026-03-01T12:00",
                "leave_at": "2026-03-01T13:00",
            },
        )
        self.service.create(
            self.actor,
            "trajectory",
            {
                "case_id": case["id"],
                "person_id": "P-2",
                "location": "Canteen",
                "enter_at": "2026-03-01T12:20",
                "leave_at": "2026-03-01T12:50",
            },
        )
        contact = self.service.list("contacts")[0]
        self.service.transition(
            self.actor,
            contact["id"],
            "begin_followup",
            {"followup_start": "2026-03-02", "due_at": "2026-03-16"},
        )
        self.assertEqual(self.service.worklist()["pending_followup_count"], 1)
        self.service.transition(
            self.actor,
            contact["id"],
            "complete_followup",
            {"outcome": "no symptoms"},
        )
        self.assertEqual(self.service.worklist()["pending_followup_count"], 0)

    def test_person_who_becomes_case_drops_from_followups(self):
        case = self._create_case()
        self.service.create(
            self.actor,
            "trajectory",
            {
                "case_id": case["id"],
                "location": "Canteen",
                "enter_at": "2026-03-01T12:00",
                "leave_at": "2026-03-01T13:00",
            },
        )
        self.service.create(
            self.actor,
            "trajectory",
            {
                "case_id": case["id"],
                "person_id": "P-2",
                "location": "Canteen",
                "enter_at": "2026-03-01T12:20",
                "leave_at": "2026-03-01T12:50",
            },
        )
        self.assertEqual(self.service.worklist()["pending_followup_count"], 1)
        self.service.create(
            Actor("admin", "admin"),
            "case",
            case_data("P-2", onset="2026-03-03"),
        )
        worklist = self.service.worklist()
        self.assertEqual(worklist["pending_followup_count"], 0)
        contact = self.service.list("contacts")[0]
        self.assertEqual(contact["status"], "withdrawn")

    def test_correct_time_withdraws_old_exposure_and_recalculates(self):
        case = self._create_case()
        first = self.service.create(
            self.actor,
            "trajectory",
            {
                "case_id": case["id"],
                "location": "Canteen",
                "enter_at": "2026-03-01T12:00",
                "leave_at": "2026-03-01T13:00",
            },
        )
        second = self.service.create(
            self.actor,
            "trajectory",
            {
                "case_id": case["id"],
                "person_id": "P-2",
                "location": "Canteen",
                "enter_at": "2026-03-01T12:30",
                "leave_at": "2026-03-01T13:30",
            },
        )
        exposure = self.service.list("exposures")[0]
        contact = self.service.list("contacts")[0]
        corrected = self.service.transition(
            self.actor,
            second["id"],
            "correct_time",
            {"enter_at": "2026-03-01T14:00", "leave_at": "2026-03-01T15:00"},
        )
        self.assertEqual(corrected["status"], "active")
        exposure = self.service.get(exposure["id"])
        self.assertEqual(exposure["status"], "withdrawn")
        contact = self.service.get(contact["id"])
        self.assertEqual(contact["status"], "withdrawn")
        self.assertEqual(self.service.worklist()["active_exposure_count"], 0)
        self.service.transition(
            self.actor,
            second["id"],
            "correct_time",
            {"enter_at": "2026-03-01T12:10", "leave_at": "2026-03-01T12:40"},
        )
        self.assertEqual(self.service.worklist()["active_exposure_count"], 1)
        contact = self.service.get(contact["id"])
        self.assertEqual(contact["status"], "identified")

    def test_invalidate_trajectory_withdraws_relations(self):
        case = self._create_case()
        self.service.create(
            self.actor,
            "trajectory",
            {
                "case_id": case["id"],
                "location": "Canteen",
                "enter_at": "2026-03-01T12:00",
                "leave_at": "2026-03-01T13:00",
            },
        )
        witness = self.service.create(
            self.actor,
            "trajectory",
            {
                "case_id": case["id"],
                "person_id": "P-2",
                "location": "Canteen",
                "enter_at": "2026-03-01T12:30",
                "leave_at": "2026-03-01T13:30",
            },
        )
        exposure = self.service.list("exposures")[0]
        self.service.transition(
            self.actor,
            witness["id"],
            "invalidate",
            {"reason": "wrong person identified"},
        )
        self.assertEqual(self.service.get(exposure["id"])["status"], "withdrawn")
        self.assertEqual(self.service.worklist()["active_exposure_count"], 0)

    def test_different_locations_do_not_overlap(self):
        case = self._create_case()
        self.service.create(
            self.actor,
            "trajectory",
            {
                "case_id": case["id"],
                "location": "Canteen",
                "enter_at": "2026-03-01T12:00",
                "leave_at": "2026-03-01T13:00",
            },
        )
        self.service.create(
            self.actor,
            "trajectory",
            {
                "case_id": case["id"],
                "person_id": "P-2",
                "location": "Library",
                "enter_at": "2026-03-01T12:30",
                "leave_at": "2026-03-01T13:30",
            },
        )
        self.assertEqual(self.service.list("exposures"), [])


class OverlapRuleTest(unittest.TestCase):
    def _trajectory(self, identifier, location, enter, leave, person="P-1", status="active"):
        return {
            "id": identifier,
            "status": status,
            "data": {
                "case_id": "c1",
                "person_id": person,
                "location": location,
                "enter_at": enter,
                "leave_at": leave,
            },
        }

    def test_touching_intervals_do_not_overlap(self):
        overlaps = compute_overlaps(
            [
                self._trajectory("t1", "A", "2026-03-01T12:00", "2026-03-01T13:00"),
                self._trajectory("t2", "A", "2026-03-01T13:00", "2026-03-01T14:00", "P-2"),
            ]
        )
        self.assertEqual(overlaps, [])

    def test_pending_trajectories_are_ignored(self):
        overlaps = compute_overlaps(
            [
                self._trajectory("t1", "A", "2026-03-01T12:00", "2026-03-01T13:00"),
                self._trajectory(
                    "t2", "A", "2026-03-01T12:30", "2026-03-01T13:30", "P-2", status="pending_time"
                ),
            ]
        )
        self.assertEqual(overlaps, [])


if __name__ == "__main__":
    unittest.main()
