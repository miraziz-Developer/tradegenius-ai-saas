from datetime import datetime, timedelta, timezone

from tradegenius import billing
from tradegenius.config import Settings
from tradegenius.db import DB
from tradegenius.worker_manager import set_ini_values, to_win


def cfg(**kw):
    c = Settings()
    c.admin_ids = {1}
    c.allowed_user_ids = set()
    c.billing_enabled = False
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def user(uid, **kw):
    return {"id": uid, "is_blocked": 0, "sub_expires_at": None, **kw}


def test_free_testing_mode_allows_everyone():
    assert billing.access(user(5), cfg()) == (True, "ok")


def test_billing_requires_subscription_but_admin_is_free():
    c = cfg(billing_enabled=True)
    assert billing.access(user(5), c) == (False, "no_subscription")
    assert billing.access(user(1), c) == (True, "admin")
    future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    assert billing.access(user(5, sub_expires_at=future), c)[0]
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert not billing.access(user(5, sub_expires_at=past), c)[0]


def test_allowlist_and_block():
    c = cfg(allowed_user_ids={7})
    assert billing.access(user(5), c) == (False, "not_allowed")
    assert billing.access(user(7), c)[0]
    assert billing.access(user(7, is_blocked=1), c) == (False, "blocked")


def test_extend_subscription_stacks(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    db.upsert_user(5)
    first = billing.extend_subscription(db, 5, 30)
    second = billing.extend_subscription(db, 5, 30)
    assert (second - first).days == 30


def test_ini_update_preserves_other_keys(tmp_path):
    p = tmp_path / "config" / "common.ini"
    p.parent.mkdir()
    p.write_bytes("[Common]\r\nLogin=1\r\n[Experts]\r\nEnabled=0\r\nProfile=7\r\n".encode("utf-16"))
    set_ini_values(str(p), "Experts", {"Enabled": "1", "AllowLiveTrading": "1"})
    text = p.read_bytes().decode("utf-16")
    assert "Login=1" in text and "Profile=7" in text
    assert "Enabled=1" in text and "Enabled=0" not in text and "AllowLiveTrading=1" in text


def test_ini_created_when_missing(tmp_path):
    p = tmp_path / "config" / "common.ini"
    set_ini_values(str(p), "Experts", {"Enabled": "1"})
    assert p.read_bytes().decode("utf-16").splitlines() == ["[Experts]", "Enabled=1"]


def test_to_win():
    assert to_win("/data/clients/acc_3/terminal64.exe") == r"Z:\data\clients\acc_3\terminal64.exe"
