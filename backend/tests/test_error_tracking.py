"""Error reports carry the method, path and stack only, and nothing is sent without a DSN."""
from app.core.observability import configure_error_tracking, scrub_error_event


def test_reports_drop_bodies_queries_cookies_users_and_local_variables():
    event = {"request": {"method": "POST", "url": "https://api/portal/import-file?token=secret",
                         "data": {"content": "invoice rows"}, "cookies": {"session": "x"},
                         "headers": {"Authorization": "Bearer iqp_x"}},
             "user": {"email": "owner@example.com"},
             "exception": {"values": [{"stacktrace": {"frames": [{"function": "f", "vars": {"row": "₹1,20,000"}}]}}]}}
    clean = scrub_error_event(event)
    assert clean["request"] == {"method": "POST", "url": "https://api/portal/import-file"}
    assert "user" not in clean
    assert clean["exception"]["values"][0]["stacktrace"]["frames"] == [{"function": "f"}]


def test_nothing_is_configured_without_a_dsn(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    assert configure_error_tracking() is False


def test_error_reports_redact_webhook_secret_from_url():
    event = {"request": {"method": "POST",
                         "url": "https://api.example/portal/zoho-webhook/disposable-test-webhook-secret?source=test"}}
    clean = scrub_error_event(event)
    assert clean["request"]["url"] == "https://api.example/portal/zoho-webhook/[REDACTED]"
