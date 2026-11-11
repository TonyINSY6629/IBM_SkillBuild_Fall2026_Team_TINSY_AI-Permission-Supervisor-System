"""Popup wiring: JSON payload, Allow via HTTP resolves the blocked request."""
import os, threading, time
os.environ["ESCALATION_OPEN_BROWSER"] = "0"
from fastapi.testclient import TestClient
from supervisor import escalation as E
from supervisor.app import app


def test_data_then_allow_resolves():
    m = E.EscalationManager(timeout_seconds=10); E.set_manager(m)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", m.request(
        "x1", "Bash", "danger_fs", "why", details={"targets": ["rm -rf .."]})))
    t.start(); time.sleep(0.2)
    c = TestClient(app)
    d = c.get("/escalation/x1/data").json()
    assert d["details"]["targets"] == ["rm -rf .."] and d["remaining"] <= 10
    assert c.post("/escalation/x1/allow", json={"note": "ok"}).json()["ok"] is True
    t.join(2)
    assert out["r"].source == "human_allow"
    assert c.get("/escalation/x1/data").status_code == 410
