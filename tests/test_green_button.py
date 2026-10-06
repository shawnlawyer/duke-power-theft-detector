import json
import time

import app


def test_green_button_secret_uses_provider_resource_and_expiry():
    payload = app.serialize_green_button_secret(
        "CONED",
        {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600, "resourceURI": "https://provider.example/resource"},
    )
    parsed = app.parse_green_button_secret(payload)
    assert parsed["resource_url"] == "https://provider.example/resource"
    assert parsed["tokens"]["refresh_token"] == "refresh"
    assert parsed["tokens"]["expires_at"] >= int(time.time()) + 3500


def test_green_button_secret_rejects_missing_provider_resource():
    try:
        app.serialize_green_button_secret("CONED", {"access_token": "access"})
    except ValueError as exc:
        assert "resource" in str(exc).lower()
    else:
        raise AssertionError("missing provider resource must be rejected")


def test_green_button_provider_aliases_are_account_bound():
    assert app.green_button_provider_key("Con Edison") == "CONED"
    assert app.green_button_provider_key("Orange and Rockland") == "ORU"
    assert app.green_button_provider_key("Duke Energy") is None


def test_green_button_secret_never_serializes_unexpected_token_fields():
    payload = json.loads(app.serialize_green_button_secret(
        "ORU", {"access_token": "a", "refresh_token": "r", "resourceURI": "https://provider.example/r", "client_secret": "do-not-store"}
    ))
    assert "client_secret" not in payload["tokens"]
