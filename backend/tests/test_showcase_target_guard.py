import pytest

from scripts.provision_portal_showcase import validate_target


def target(monkeypatch, database, explicit=None):
    monkeypatch.setenv("INVOICEIQ_PORTAL_SHOWCASE", "true")
    monkeypatch.setenv("DATABASE_URL", f"postgresql+asyncpg://u:p@db.example/{database}")
    if explicit is None:
        monkeypatch.delenv("INVOICEIQ_SHOWCASE_DATABASE", raising=False)
    else:
        monkeypatch.setenv("INVOICEIQ_SHOWCASE_DATABASE", explicit)


def test_default_guard_accepts_only_disposable_names(monkeypatch):
    target(monkeypatch, "invoiceiq_sample")
    validate_target()
    target(monkeypatch, "invoiceiq")
    with pytest.raises(RuntimeError, match="restricted"):
        validate_target()
    target(monkeypatch, "production")
    with pytest.raises(RuntimeError, match="restricted"):
        validate_target()


def test_deployment_can_opt_in_to_exactly_one_named_database(monkeypatch):
    target(monkeypatch, "invoiceiq", explicit="invoiceiq")
    validate_target()
    target(monkeypatch, "production", explicit="invoiceiq")
    with pytest.raises(RuntimeError, match="restricted"):
        validate_target()


def test_showcase_flag_is_still_required(monkeypatch):
    target(monkeypatch, "invoiceiq_sample")
    monkeypatch.setenv("INVOICEIQ_PORTAL_SHOWCASE", "false")
    with pytest.raises(RuntimeError, match="explicitly"):
        validate_target()
