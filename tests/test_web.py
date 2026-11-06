from fastapi.testclient import TestClient

from alerts.notifier import Notifier
from core.db import DB
from web.server import AppState, create_app

from .test_pipeline import make, make_page


class FakeScheduler:
    paused = False
    running_sweep = False
    ran = 0

    def run_now(self):
        self.ran += 1

    def resume(self):
        self.paused = False

    def pause(self):
        self.paused = True


def client_with_data():
    p, db, sent, cfg = make({"pistonheads": [(200, make_page("Nissan Silvia S15 Spec-R standard, reg Y123 ABC", 18500))]})
    p.sweep()
    state = AppState(db, p, FakeScheduler(), Notifier(lambda: state.cfg(), sent.ntfy_send, sent.toast_send))
    return TestClient(create_app(state)), state, sent


def test_first_run_goes_to_three_question_setup_and_sends_test_alert():
    c, state, sent = client_with_data()
    state.cfg()["setup_complete"] = False
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    page = c.get("/setup").text
    assert page.count('<label') >= 3 and "ntfy" in page and state.cfg()["alerts"]["ntfy_topic"] in page
    assert c.get("/qr.svg").headers["content-type"].startswith("image/svg")
    n_before = len(sent.ntfy)
    r = c.post("/setup", data={"postcode": "le1 1aa", "budget_min": "12,000", "budget_max": "18k"}, follow_redirects=False)
    assert r.headers["location"] == "/first-run"
    cfg = state.cfg()
    assert cfg["setup_complete"] and cfg["owner"]["home_postcode"] == "LE1 1AA"
    assert cfg["budget"] == {"min_gbp": 12000, "max_gbp": 18000}
    assert len(sent.ntfy) == n_before + 1 and sent.ntfy[-1]["title"] == "SX200 Tracker is set up"
    assert state.scheduler.ran == 1
    assert "First check" in c.get("/progress").text or "Checking" in c.get("/progress").text


def test_dashboard_shows_card_with_reasons_mot_link_and_actions():
    c, state, _ = client_with_data()
    state.cfg()["setup_complete"] = True
    html = c.get("/").text
    assert "Nissan Silvia S15 Spec-R standard" in html
    assert "£18,500" in html and "Score" in html
    assert "Spec-R or SR20DET turbo named" in html
    assert "check-mot.service.gov.uk/results?registration=Y123ABC" in html
    assert "1 of 1 sources working" in html
    car_id = state.db.one("SELECT id FROM cars WHERE in_scope=1")["id"]
    assert c.post(f"/cars/{car_id}/shortlist").status_code == 200
    assert "Nissan Silvia S15 Spec-R" in c.get("/?view=shortlist").text
    assert "Nissan Silvia S15 Spec-R" not in c.get("/cars?view=new").text
    c.post(f"/cars/{car_id}/notes", data={"notes": "Ring Saturday"})
    assert state.db.one("SELECT owner_notes FROM cars WHERE id=?", (car_id,))["owner_notes"] == "Ring Saturday"
    assert "PistonHeads" in c.get("/sources").text
    assert c.post("/problem-report").headers["content-type"] == "application/zip"


def test_settings_save_and_disable_source():
    c, state, _ = client_with_data()
    state.cfg()["setup_complete"] = True
    assert "Sites to check" in c.get("/settings").text
    c.post("/settings", data={"postcode": "M1 1AA", "budget_min": "9000", "budget_max": "21000", "min_score": "70",
                              "ntfy_enabled": "on"})
    cfg = state.cfg()
    assert cfg["alerts"]["alert_min_score"] == 70 and cfg["budget"]["max_gbp"] == 21000
    assert "pistonheads" in cfg["sources"]["disabled"]  # its box was not ticked
    assert cfg["alerts"]["windows_enabled"] is False
