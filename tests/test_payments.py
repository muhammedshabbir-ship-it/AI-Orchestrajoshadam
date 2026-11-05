import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest
from core import db, ledger, security, finance, authz
from core.money import to_cents


@pytest.fixture(autouse=True)
def fresh():
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(db.DB_PATH + ext):
            os.remove(db.DB_PATH + ext)
    db.init()
    db.set_setting("mode", "TEST"); db.set_setting("ceiling_cents", str(10**11))
    for n, r in [("adm", "admin"), ("app", "approver"), ("op", "operator"), ("vw", "viewer")]:
        security.create_user(n, "passwordpassword", r)
    ledger.record_deposit(to_cents("100000"), "R", "", "adm")
    ledger.move_to_vault(to_cents("100000"), "adm")
    yield


def draft(method, actor="adm", amt="50", **kw):
    return ledger.create_release("Jane Citizen", kw.get("bsb", ""), kw.get("account", ""), kw.get("payid", ""),
                                 to_cents(amt), "", actor, confirmed_warnings=True, method=method,
                                 biller_code=kw.get("biller", ""), crn=kw.get("crn", ""), category="Bills")


def test_all_methods_draft_and_approve():
    ids = [draft("BANK", bsb="062-000", account="12345678"), draft("PAYID_EMAIL", payid="jane@example.com"),
           draft("PAYID_MOBILE", payid="0412 345 678"), draft("PAYID_ABN", payid="51 824 753 556"),
           draft("BPAY", biller="12345", crn="987654321")]
    for i in ids:
        ledger.approve_release(i, "adm")
    assert {r["method"] for r in ledger.releases()} == {"BANK", "PAYID_EMAIL", "PAYID_MOBILE", "PAYID_ABN", "BPAY"}
    assert ledger.balance_cents() == to_cents("100000") - 5 * to_cents("50")
    assert any(r["payid"] == "0412345678" for r in ledger.releases())


@pytest.mark.parametrize("method,kw", [("PAYID_EMAIL", {"payid": "nope"}), ("PAYID_MOBILE", {"payid": "123"}),
                                       ("PAYID_ABN", {"payid": "11111111111"}), ("BPAY", {"biller": "1", "crn": "x"}),
                                       ("BANK", {"bsb": "12", "account": "1"})])
def test_bad_details_rejected(method, kw):
    with pytest.raises(ValueError):
        draft(method, **kw)


def test_roles():
    with pytest.raises(authz.NotAuthorised):
        draft("PAYID_EMAIL", actor="vw", payid="a@b.co")
    rid = draft("PAYID_EMAIL", actor="op", payid="a@b.co")
    with pytest.raises(authz.NotAuthorised):
        ledger.approve_release(rid, "op")
    with pytest.raises(authz.NotAuthorised):
        ledger.approve_release(rid, "agent:Treasury")
    ledger.approve_release(rid, "app")


def test_typed_amount_and_dual_control_and_limits():
    db.set_setting("confirm_cents", to_cents("100"))
    rid = draft("PAYID_EMAIL", payid="a@b.co", amt="500")
    with pytest.raises(ValueError, match="amount"):
        ledger.approve_release(rid, "adm")
    with pytest.raises(ValueError):
        ledger.approve_release(rid, "adm", typed_amount="499")
    db.set_setting("dual_control_cents", to_cents("100"))
    with pytest.raises(ValueError, match="[Dd]ual"):
        ledger.approve_release(rid, "adm", typed_amount="500")
    ledger.approve_release(rid, "app", typed_amount="500")
    db.set_setting("max_release_cents", to_cents("100"))
    with pytest.raises(ValueError):
        draft("PAYID_EMAIL", payid="a@b.co", amt="101")
    db.set_setting("max_release_cents", 0); db.set_setting("monthly_limit_cents", to_cents("600"))
    with pytest.raises(ValueError):
        draft("PAYID_EMAIL", payid="a@b.co", amt="200")


def test_cooling_off_and_saved_only():
    db.set_setting("cooling_minutes", 30)
    rid = draft("PAYID_EMAIL", payid="a@b.co")
    with pytest.raises(ValueError, match="[Cc]ooling"):
        ledger.approve_release(rid, "adm")
    db.x("UPDATE releases SET ts=ts-4000")
    ledger.approve_release(rid, "adm")
    db.set_setting("payee_mode", "saved_only")
    with pytest.raises(ValueError):
        draft("PAYID_EMAIL", payid="new@b.co")
    draft("PAYID_EMAIL", payid="a@b.co")  # saved after approval


def test_totp():
    assert security.totp_code("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", 59)[-6:] == "287082"
    sec = security.totp_new_secret()
    security.enable_totp("adm", sec, security.totp_code(sec))
    rid = draft("PAYID_EMAIL", payid="a@b.co")
    with pytest.raises(ValueError, match="[Aa]uthenticator"):
        ledger.approve_release(rid, "adm")
    db.set_setting("totp_last_adm", "0")
    ledger.approve_release(rid, "adm", totp_code=security.totp_code(sec))
    with pytest.raises(ValueError):  # replay
        security.totp_check("adm", security.totp_code(sec)) or (_ for _ in ()).throw(ValueError("replay"))


def test_finance_reports():
    ledger.approve_release(draft("BPAY", biller="12345", crn="99"), "adm")
    assert finance.period_summary()["count"] == 1
    assert finance.by_method()[0]["method"].startswith("BPAY")
    assert finance.by_category()[0]["category"] == "Bills"
    finance.set_budget("Bills", to_cents("100"), "adm")
    assert finance.budget_usage()[0]["pct"] == 50.0
    assert finance.reconcile()["in_flight_cents"] == to_cents("50")
    with pytest.raises(authz.NotAuthorised):
        finance.set_budget("Bills", 1, "op")


def test_provider_states_and_send(monkeypatch):
    from core import providers, secrets
    monkeypatch.delenv("PROVIDER_API_KEY", raising=False)
    assert providers.status()[0] == "manual"
    providers.save_config("zepto", "sandbox", "ACC1", "", "adm")
    assert providers.status()[0] == "missing_credentials"
    monkeypatch.setenv("PROVIDER_API_KEY", "key-12345678")
    assert providers.status()[0] == "no_adapter"
    sent = []

    @providers.register("zepto")
    class Fake(providers.Adapter):
        name = "zepto"
        def check(self): return True, "ok"
        def send(self, r): sent.append(r["id"]); return "PAY-1"
    try:
        assert providers.status()[0] == "ready"
        rid = draft("PAYID_EMAIL", payid="a@b.co")
        ledger.approve_release(rid, "adm")
        with pytest.raises(authz.NotAuthorised):
            providers.send_release(rid, "op")
        assert providers.send_release(rid, "adm") == "PAY-1"
        with pytest.raises(ValueError):
            providers.send_release(rid, "adm")  # no double send
        assert sent == [rid] and ledger.releases("EXPORTED")[0]["batch_id"] == "PAY-1"
        db.set_setting("provider_env", "live")
        assert providers.status()[0] == "missing_credentials"  # TEST mode needs sandbox
    finally:
        providers.ADAPTERS.pop("zepto", None)


def test_provider_failure_sends_nothing(monkeypatch):
    from core import providers
    monkeypatch.setenv("PROVIDER_API_KEY", "key-12345678")
    providers.save_config("monoova", "sandbox", "A", "", "adm")

    @providers.register("monoova")
    class Bad(providers.Adapter):
        def check(self): return True, ""
        def send(self, r): raise RuntimeError("declined")
    try:
        rid = draft("PAYID_EMAIL", payid="a@b.co"); ledger.approve_release(rid, "adm")
        with pytest.raises(ValueError, match="Nothing was sent"):
            providers.send_release(rid, "adm")
        assert ledger.releases("APPROVED")  # still approved, retry possible
    finally:
        providers.ADAPTERS.pop("monoova", None)
