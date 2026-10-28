"""28.10.2026: names for scheduled monitoring; project filter on the wall; CENTOR_QAS wording."""
import os


def test_schedule_name_is_kept_and_cleaned():
    from core import schedules
    c = schedules._normalise({"mode": "hours", "every": 1, "name": "  Full <b>sweep</b>\n "})
    assert c["name"] == "Full bsweep/b"
    assert schedules._normalise({})["name"] == ""
    assert len(schedules._normalise({"name": "x" * 200})["name"]) == 60


def test_run_names_for_profiles_schedules_and_others():
    from core import run_names
    names = ({"prd-quick": "S4P quick check"}, {"CEAT S4P": "Full sweep"})
    assert run_names.name_for({"profile_id": "prd-quick", "trigger": "schedule"}, names) == "S4P quick check"
    assert run_names.name_for({"profile_id": None, "trigger": "schedule", "system": "CEAT S4P"}, names) == "Full sweep"
    assert run_names.name_for({"profile_id": None, "trigger": "catch-up", "system": "CEAT S4P"}, names) == "Full sweep"
    assert run_names.name_for({"profile_id": None, "trigger": "manual-system", "system": "CEAT S4P"}, names) == ""
    assert run_names.name_for({"profile_id": "gone", "trigger": "schedule"}, names) == "gone"


def test_wall_has_a_project_filter():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(here, "dashboard", "static", "wall.html"), encoding="utf-8").read()
    assert 'id="projbar"' in html and "/api/projects" in html and "inProject(" in html
    assert "onclick" not in html.split('id="projbar"')[1].split("</div>")[0]


