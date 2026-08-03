from __future__ import annotations

from app import config
from app.ocado import session as ocado_session
from app.ocado.auth import AuthState
from app.ocado.ledger import read_ledger, write_ledger
from app.ocado.sync import CartLedger, LedgerLine


def test_named_account_config_parses_env(monkeypatch):
    monkeypatch.setenv("OCADO_ACCOUNTS", "main,backup")
    monkeypatch.setenv("OCADO_MAIN_LABEL", "Main shop")
    monkeypatch.setenv("OCADO_MAIN_EMAIL", "main@example.com")
    monkeypatch.setenv("OCADO_MAIN_PASSWORD", "main-secret")
    monkeypatch.setenv("OCADO_BACKUP_LABEL", "Backup shop")
    monkeypatch.setenv("OCADO_BACKUP_EMAIL", "backup@example.com")
    monkeypatch.setenv("OCADO_BACKUP_PASSWORD", "backup-secret")

    accounts = config._configured_ocado_accounts()

    assert [account.id for account in accounts] == ["main", "backup"]
    assert accounts[0].label == "Main shop"
    assert accounts[0].email == "main@example.com"
    assert accounts[1].password == "backup-secret"


def test_legacy_account_config_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("OCADO_ACCOUNTS", raising=False)
    monkeypatch.setenv("OCADO_EMAIL", "legacy@example.com")
    monkeypatch.setenv("OCADO_PASSWORD", "legacy-secret")

    (account,) = config._configured_ocado_accounts()

    assert account.id == "default"
    assert account.email == "legacy@example.com"
    assert account.password == "legacy-secret"


def test_account_runtime_uses_separate_session_paths(tmp_path, monkeypatch):
    accounts = (
        config.OcadoAccountConfig(id="main", label="Main", email="a", password="b"),
        config.OcadoAccountConfig(id="backup", label="Backup", email="c", password="d"),
    )
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "OCADO_ACCOUNTS", accounts)
    monkeypatch.setattr(config, "DEFAULT_OCADO_ACCOUNT_ID", "main")
    ocado_session._RUNTIMES.clear()

    main = ocado_session.get_account_runtime("main")
    backup = ocado_session.get_account_runtime("backup")

    assert main.session.jar_path == tmp_path / "ocado" / "accounts" / "main" / "session.json"
    assert backup.session.jar_path == tmp_path / "ocado" / "accounts" / "backup" / "session.json"
    assert main.auth.profile_dir != backup.auth.profile_dir
    assert main.auth.email == "a"
    assert backup.auth.password == "d"

    main.auth.state = AuthState.READY
    assert backup.auth.state == AuthState.LOGGED_OUT


def test_ledger_is_isolated_by_account(factory):
    write_ledger(
        factory,
        CartLedger(lines=(LedgerLine(sku="sku-a", quantity=2, name="A"),)),
        account_id="main",
        week_start="2026-08-01",
    )
    write_ledger(
        factory,
        CartLedger(lines=(LedgerLine(sku="sku-a", quantity=5, name="A"),)),
        account_id="backup",
        week_start="2026-08-08",
    )

    main = read_ledger(factory, account_id="main")
    backup = read_ledger(factory, account_id="backup")

    assert main.quantities == {"sku-a": 2}
    assert main.week_start == "2026-08-01"
    assert backup.quantities == {"sku-a": 5}
    assert backup.week_start == "2026-08-08"
