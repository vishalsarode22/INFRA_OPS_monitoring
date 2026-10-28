"""A park the tool set itself after a refused logon ends once an RFC logon works again."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _db():
    from core import store
    store.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.sqlite")


def test_automatic_credentials_park_is_cleared():
    _db()
    from core import alert_control, reachability
    alert_control.park("SYS1", reason="automatic: CREDENTIALS", by="system", auto=True)
    assert alert_control.parked("SYS1")
    reachability.auto_unpark_after_logon("SYS1")
    assert not alert_control.parked("SYS1")


def test_a_park_set_by_a_person_stays():
    _db()
    from core import alert_control, reachability
    alert_control.park("SYS2", reason="maintenance by Basis", by="alex", auto=False)
    reachability.auto_unpark_after_logon("SYS2")
    assert alert_control.parked("SYS2")
    alert_control.park("SYS3", reason="automatic: SYSTEM_DOWN", by="system", auto=True)
    reachability.auto_unpark_after_logon("SYS3")
    assert alert_control.parked("SYS3")


def test_switch_off():
    _db()
    from core import alert_control, reachability
    alert_control.park("SYS4", reason="automatic: CREDENTIALS", by="system", auto=True)
    os.environ["IBO_AUTO_UNPARK"] = "0"
    try:
        reachability.auto_unpark_after_logon("SYS4")
    finally:
        os.environ.pop("IBO_AUTO_UNPARK", None)
    assert alert_control.parked("SYS4")
