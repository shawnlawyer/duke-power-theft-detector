import base64
import csv
import json
import re
import zipfile
from io import BytesIO, StringIO
from datetime import date as ddate, datetime, timedelta
from pathlib import Path

import app
import pandas as pd
import pyotp
import pytest


FIXTURE = Path(__file__).parent / "fixtures" / "sample_interval.xml"
DUKE_FIXTURE = Path(__file__).parent / "fixtures" / "duke_interval_block.xml"
CSV_FIXTURE = Path(__file__).parent / "fixtures" / "utility_interval.csv"


def configure_tmp_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "INPUT_DIR", tmp_path / "input")
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "output" / "power-history.db")
    monkeypatch.setattr(app, "DATABASE_URL", "")
    monkeypatch.delenv("POWER_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("POWER_ENV", "test")
    monkeypatch.setenv("POWER_APP_SECRET", "home-energy-watch-test-secret-0001")
    monkeypatch.setenv("POWER_AUDIT_SIGNING_KEY", "home-energy-watch-test-audit-key-0001")
    monkeypatch.setenv("POWER_PUBLIC_BASE_URL", "https://app.homeenergywatch.com")
    monkeypatch.setenv("POWER_MARKETING_BASE_URL", "https://homeenergywatch.com")
    monkeypatch.setenv("POWER_STAFF_MFA_REQUIRED", "false")
    monkeypatch.delenv("POWER_DATA_DELETION_ENABLED", raising=False)
    monkeypatch.delenv("POWER_DATA_DELETION_POLICY_VERSION", raising=False)
    monkeypatch.delenv("POWER_BILLING_ENABLED", raising=False)
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("STRIPE_PRICE_HOME", raising=False)
    monkeypatch.delenv("STRIPE_PRICE_REVIEW", raising=False)
    monkeypatch.delenv("STRIPE_PRICE_AGENCY", raising=False)
    monkeypatch.setenv(
        "POWER_DATA_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(b"home-energy-watch-test-key-0001!").decode("ascii"),
    )
    app.web_app.config["CSRF_ENFORCE_TESTS"] = False
    app.SCHEMA_READY_TARGETS.clear()
    app.ensure_data_dirs()


def stub_utility_lookup(monkeypatch, company="Duke Energy Progress, LLC"):
    monkeypatch.setattr(
        app,
        "lookup_energy_company_by_zip",
        lambda zip_code, address=None: {
            "energy_company": company,
            "eia_utility_id": "3046",
            "zip_code": str(zip_code)[:5],
            "match_address": address or "North Carolina",
            "match_basis": "service address" if address else "ZIP code",
        },
    )


def bootstrap_staff():
    if app.count_staff_users() == 0:
        app.create_first_staff_user("commission@example.gov", "Commissioner One", "test-password-123")


def sign_in(client):
    bootstrap_staff()
    return client.post(
        "/login",
        data={"email": "commission@example.gov", "password": "test-password-123"},
        follow_redirects=False,
    )


def customer_sign_in(client, email="owner@example.com", password="customer-password-123"):
    return client.post(
        "/customer/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )


def authorize_account(account_number, email=None):
    normalized_account = app.normalize_account_number(account_number)
    safe_account = re.sub(r"[^a-z0-9]+", "-", normalized_account.lower()).strip("-") or "account"
    customer_email = email or f"{safe_account}@example.com"
    customer = app.get_customer_user_by_email(customer_email)
    if customer is None:
        customer = app.create_customer_user(
            customer_email,
            f"Owner {normalized_account}",
            "customer-password-123",
        )
    if app.find_account(normalized_account) is None:
        app.save_account_profile(normalized_account, display_name=f"Account {normalized_account}")
    app.add_account_access_email(
        normalized_account,
        customer_email,
        full_name=str(customer["full_name"]),
        access_level="Manager",
    )
    return app.grant_account_data_authorization(normalized_account, int(customer["id"]))


def make_summary(rows):
    frame = pd.DataFrame(rows)
    frame["date"] = frame["date"].map(ddate.fromisoformat)
    return frame.set_index("date")


def comparison_csv(values):
    rows = ["interval_start,interval_end,usage_kwh"]
    for start, end, usage_kwh in values:
        rows.append(f"{start},{end},{usage_kwh}")
    return ("\n".join(rows) + "\n").encode()


def token_from_latest_email():
    assert app.EMAIL_OUTBOX
    match = re.search(r"[?&]token=([A-Za-z0-9_-]+)", app.EMAIL_OUTBOX[-1]["text_body"])
    assert match is not None
    return match.group(1)


def enroll_staff_mfa(staff_user_id):
    enrollment = app.begin_staff_mfa_enrollment(int(staff_user_id))
    result = app.confirm_staff_mfa_enrollment(
        int(staff_user_id),
        pyotp.TOTP(enrollment["secret"]).now(),
    )
    return enrollment, result


def test_analyze_interval_file_flags_expected_day(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)

    summary, baseline, report_path = app.analyze_interval_file(FIXTURE)

    assert round(baseline, 2) == 0.95
    assert bool(summary.loc[summary.index[0], "suspicious"]) is True
    assert bool(summary.loc[summary.index[1], "suspicious"]) is False
    assert report_path.exists()


def test_analyze_interval_file_writes_ranked_json_report(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)

    _, _, report_path = app.analyze_interval_file(FIXTURE)
    json_path = report_path.with_suffix(".json")
    payload = json.loads(json_path.read_text())

    assert json_path.exists()
    assert payload["input_file"] == FIXTURE.name
    assert payload["report_file"] == report_path.name
    assert [item["label"] for item in payload["report_files"]] == ["CSV", "JSON"]
    assert payload["ranked_suspicious_days"][0]["date"] == "2024-01-01"
    assert payload["ranked_suspicious_days"][0]["severity_rank"] == 1
    assert payload["ranked_suspicious_days"][0]["alert_count"] >= 1
    assert report_path.name in app.list_report_files()
    assert json_path.name in app.list_report_files()


def test_analyze_interval_file_writes_weather_context_to_artifacts(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)

    def fake_load_day_weather(account_number, weather_date, tz_name):
        assert account_number == "acct-1"
        assert weather_date == "2024-01-01"
        assert tz_name == "America/New_York"
        return {
            "available": True,
            "location_name": "Charlotte, North Carolina, United States",
            "summary": {
                "high_temp_f": 96.4,
                "low_temp_f": 77.2,
                "high_apparent_f": 103.1,
                "precipitation_in": 0.02,
                "max_wind_mph": 12.5,
                "conditions": "Clear",
            },
            "hourly": [],
        }

    monkeypatch.setattr(app, "load_day_weather", fake_load_day_weather)

    _, _, report_path = app.analyze_interval_file(
        FIXTURE,
        output_path=tmp_path / "output" / "weather-report.csv",
        account_number="acct-1",
    )
    payload = json.loads(report_path.with_suffix(".json").read_text())
    csv_rows = pd.read_csv(report_path)

    flagged_day = payload["ranked_suspicious_days"][0]
    assert flagged_day["weather_context"]["available"] is True
    assert flagged_day["weather_context"]["signals"] == ["unusual_heat"]
    assert flagged_day["weather_context"]["effect"] == "plausible_explanation"
    assert "hot weather" in flagged_day["weather_context"]["summary"].lower()

    csv_row = csv_rows[csv_rows["date"] == "2024-01-01"].iloc[0]
    assert csv_row["weather_signals"] == "unusual_heat"
    assert "hot weather" in csv_row["weather_context"].lower()
    assert csv_row["weather_high_temp_f"] == 96.4


def test_compute_alert_events_finds_midnight_spike():
    frame = app.parse_duke_xml(FIXTURE)
    events = app.compute_alert_events(frame, min_kw=1.0, alert_multiplier=1.5, jump_kw=0.5)

    assert events
    assert events[0]["date"] == "2024-01-01"
    assert "midnight" in events[0]["reasons"] or "overnight" in events[0]["reasons"]


def test_parse_duke_interval_block_uses_block_duration_and_kwh_units():
    frame = app.parse_duke_xml(DUKE_FIXTURE)

    assert len(frame) == 3
    assert int(frame.loc[0, "duration_s"]) == 900
    assert round(float(frame.loc[0, "wh"]), 1) == 350.0
    assert round(float(frame.loc[0, "kw"]), 2) == 1.40


def test_detect_utility_feed_adapter_identifies_green_button_espi():
    detected = app.detect_utility_feed_adapter(DUKE_FIXTURE)

    assert detected["adapter_id"] == "green_button_espi"
    assert detected["display_name"] == "Green Button ESPI XML"
    assert detected["standard_label"] == "NAESB REQ.21 ESPI / Green Button"


def test_detect_utility_feed_adapter_identifies_duke_style_interval_xml():
    detected = app.detect_utility_feed_adapter(FIXTURE)

    assert detected["adapter_id"] == "duke_style_interval_xml"
    assert detected["display_name"] == "Duke-style interval XML"
    assert detected["format_label"] == "IntervalBlock / IntervalReading XML"


def test_detect_utility_feed_adapter_identifies_interval_csv():
    detected = app.detect_utility_feed_adapter(CSV_FIXTURE)

    assert detected["adapter_id"] == "utility_interval_csv"
    assert detected["display_name"] == "Utility interval CSV"
    assert detected["format_label"] == "Timestamped interval CSV"


def test_parse_interval_csv_uses_kwh_values_and_end_timestamps():
    parsed = app.parse_interval_file(CSV_FIXTURE)
    frame = parsed.frame

    assert parsed.adapter.adapter_id == "utility_interval_csv"
    assert len(frame) == 3
    assert int(frame.loc[0, "duration_s"]) == 900
    assert round(float(frame.loc[0, "wh"]), 1) == 350.0
    assert round(float(frame.loc[0, "kw"]), 2) == 1.40


def test_supported_utility_adapter_registry_lists_current_formats():
    supported = app.list_supported_utility_adapters()
    adapter_ids = {item["adapter_id"] for item in supported}

    assert adapter_ids == {"green_button_espi", "duke_style_interval_xml", "utility_interval_csv"}
    assert any(item["provider_label"] == "Green Button utility exports" for item in supported)
    assert all(item["status"] == "supported" for item in supported)


def test_energy_company_lookup_uses_service_location_and_territory(monkeypatch):
    requested_urls = []

    def fake_fetch_json(url):
        requested_urls.append(url)
        if "findAddressCandidates" in url:
            return {
                "candidates": [
                    {
                        "score": 100,
                        "address": "28205, Charlotte, North Carolina",
                        "attributes": {"Region": "NC", "Postal": "28205"},
                        "location": {"x": -80.788139, "y": 35.219597},
                    }
                ]
            }
        if "/FeatureServer/2/query" in url:
            return {
                "features": [
                    {
                        "attributes": {
                            "OWNER_1": "Duke Energy Carolinas",
                            "EIA_UTIL_1": 5416,
                        }
                    }
                ]
            }
        return {"features": []}

    monkeypatch.setattr(app, "fetch_json", fake_fetch_json)

    match = app.lookup_energy_company_by_zip("28205", "123 Main Street, Charlotte")

    assert match["energy_company"] == "Duke Energy Carolinas, LLC"
    assert match["eia_utility_id"] == "5416"
    assert match["zip_code"] == "28205"
    assert match["match_basis"] == "service address"
    assert len(requested_urls) == 4


def test_energy_company_lookup_asks_for_address_when_zip_crosses_service_areas(monkeypatch):
    def fake_fetch_json(url):
        if "findAddressCandidates" in url:
            return {
                "candidates": [
                    {
                        "score": 100,
                        "address": "27587, Wake Forest, North Carolina",
                        "attributes": {"Region": "NC", "Postal": "27587"},
                        "location": {"x": -78.51, "y": 35.98},
                    }
                ]
            }
        if "/FeatureServer/0/query" in url:
            return {"features": [{"attributes": {"OWNER": "Wake Forest Town of", "EIA_UTILIT": 19974}}]}
        if "/FeatureServer/2/query" in url:
            return {"features": [{"attributes": {"OWNER_1": "Duke Energy Progress", "EIA_UTIL_1": 3046}}]}
        return {"features": []}

    monkeypatch.setattr(app, "fetch_json", fake_fetch_json)

    try:
        app.lookup_energy_company_by_zip("27587")
    except ValueError as exc:
        assert "crosses electric service areas" in str(exc)
    else:
        raise AssertionError("Expected an ambiguous ZIP code to require a street address.")


def test_history_database_isolates_accounts_and_dedupes_within_each_account(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)

    first = tmp_path / "input" / "history-1.xml"
    second = tmp_path / "input" / "history-2.xml"
    first.write_bytes(FIXTURE.read_bytes())
    second.write_bytes(FIXTURE.read_bytes())

    app.import_interval_file_to_db(first, account_number="acct-1")
    app.import_interval_file_to_db(second, account_number="acct-1")
    app.import_interval_file_to_db(first, account_number="acct-2")

    assert app.count_imported_files("acct-1") == 2
    assert app.count_imported_files("acct-2") == 1
    assert len(app.load_intervals_from_db("acct-1")) == 6
    assert len(app.load_intervals_from_db("acct-2")) == 6


def test_import_interval_file_returns_adapter_metadata_when_file_is_unchanged(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)

    sample = tmp_path / "input" / "history.xml"
    sample.write_bytes(FIXTURE.read_bytes())

    imported = app.import_interval_file_to_db(sample, account_number="acct-1")
    skipped = app.import_interval_file_to_db(sample, account_number="acct-1")

    assert imported["adapter_id"] == "duke_style_interval_xml"
    assert imported["adapter_name"] == "Duke-style interval XML"
    assert skipped["imported"] is False
    assert skipped["adapter_id"] == "duke_style_interval_xml"
    assert skipped["adapter_name"] == "Duke-style interval XML"


def test_baseline_date_uses_selected_reference_day():
    frame = app.parse_duke_xml(FIXTURE)
    summary = app.compute_daily_summary(frame, night_start_str="02:00", night_end_str="04:00")

    flagged, baseline = app.flag_suspicious_days(
        summary,
        min_night_kw=1.0,
        night_multiplier=2.0,
        baseline_date="2024-01-02",
    )

    expected = float(summary.loc[ddate.fromisoformat("2024-01-02"), "night_avg_kw"])
    assert round(baseline, 3) == round(expected, 3)
    assert bool(flagged.loc[ddate.fromisoformat("2024-01-01"), "suspicious"]) is True


def test_build_interval_comparison_aligns_year_over_year_months():
    left_summary = make_summary(
        [
            {"date": "2024-01-01", "total_kwh": 100.0, "night_avg_kw": 0.8, "suspicious": False},
            {"date": "2024-01-02", "total_kwh": 110.0, "night_avg_kw": 0.9, "suspicious": True},
            {"date": "2024-02-01", "total_kwh": 120.0, "night_avg_kw": 1.0, "suspicious": True},
            {"date": "2024-02-02", "total_kwh": 130.0, "night_avg_kw": 1.1, "suspicious": False},
        ]
    )
    right_summary = make_summary(
        [
            {"date": "2025-01-01", "total_kwh": 130.0, "night_avg_kw": 1.1, "suspicious": True},
            {"date": "2025-01-02", "total_kwh": 140.0, "night_avg_kw": 1.2, "suspicious": True},
            {"date": "2025-02-01", "total_kwh": 150.0, "night_avg_kw": 1.4, "suspicious": True},
            {"date": "2025-02-02", "total_kwh": 160.0, "night_avg_kw": 1.5, "suspicious": True},
        ]
    )

    comparison = app.build_interval_comparison(
        left_summary=left_summary,
        right_summary=right_summary,
        left_baseline=0.95,
        right_baseline=1.3,
        left_label="earlier.xml",
        right_label="later.xml",
    )

    assert comparison["alignment_mode"] == "year_over_year"
    assert comparison["offset_months"] == 12
    assert comparison["overview"]["matched_periods"] == 2
    assert comparison["left_only_periods"] == []
    assert comparison["right_only_periods"] == []
    assert comparison["rows"][0]["comparison_label"] == "Jan 2024 vs Jan 2025"
    assert round(float(comparison["rows"][0]["total_kwh_delta"]), 1) == 60.0
    assert round(float(comparison["rows"][0]["overnight_baseline_delta_kw"]), 2) == 0.30
    assert comparison["rows"][0]["flagged_nights_delta"] == 1


def test_build_interval_comparison_excludes_unmatched_months_in_mismatched_ranges():
    left_summary = make_summary(
        [
            {"date": "2024-01-01", "total_kwh": 90.0, "night_avg_kw": 0.7, "suspicious": False},
            {"date": "2024-02-01", "total_kwh": 110.0, "night_avg_kw": 0.9, "suspicious": True},
            {"date": "2024-03-01", "total_kwh": 120.0, "night_avg_kw": 1.0, "suspicious": True},
        ]
    )
    right_summary = make_summary(
        [
            {"date": "2025-02-01", "total_kwh": 135.0, "night_avg_kw": 1.2, "suspicious": True},
            {"date": "2025-03-01", "total_kwh": 150.0, "night_avg_kw": 1.4, "suspicious": True},
            {"date": "2025-04-01", "total_kwh": 165.0, "night_avg_kw": 1.5, "suspicious": True},
        ]
    )

    comparison = app.build_interval_comparison(
        left_summary=left_summary,
        right_summary=right_summary,
        left_baseline=0.9,
        right_baseline=1.3,
        left_label="left.xml",
        right_label="right.xml",
    )

    assert comparison["alignment_mode"] == "year_over_year"
    assert comparison["overview"]["matched_periods"] == 2
    assert comparison["left_only_periods"] == ["Jan 2024"]
    assert comparison["right_only_periods"] == ["Apr 2025"]
    assert [row["comparison_label"] for row in comparison["rows"]] == [
        "Feb 2024 vs Feb 2025",
        "Mar 2024 vs Mar 2025",
    ]


def test_household_profile_is_saved_with_account(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)

    app.save_account_profile("acct-1", display_name="Test Home", baseline_date="2024-01-02")
    profile = app.save_household_profile(
        "acct-1",
        {
            "address": "123 Main St",
            "occupant_count": "3",
            "year_built": "1989",
            "square_footage": "2200",
            "heating_system": "Heat pump",
            "cooling_system": "Central air",
            "water_heater": "Electric tank",
            "notes": "Pool pump and garage fridge",
        },
    )

    assert profile["address"] == "123 Main St"
    assert profile["occupant_count"] == 3
    assert profile["year_built"] == 1989
    assert profile["heating_system"] == "Heat pump"
    assert profile["latitude"] is None


def test_account_access_emails_are_saved_per_account(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)

    app.save_account_profile("acct-1", display_name="Main house")
    app.add_account_access_email("acct-1", "Owner@example.com", full_name="Home Owner")
    app.add_account_access_email("acct-1", "advisor@example.com", full_name="Energy Advisor")
    app.add_account_access_email("acct-2", "other@example.com", full_name="Other Account")

    access = app.list_account_access_emails("acct-1")

    assert [item["email"] for item in access] == ["advisor@example.com", "owner@example.com"]
    assert access[1]["full_name"] == "Home Owner"


def test_removing_account_access_revokes_permission_and_erases_saved_utility_access(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    customer = app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    app.save_account_profile("acct-1", energy_company="Duke Energy Carolinas, LLC")
    access = app.add_account_access_email(
        "acct-1",
        "owner@example.com",
        full_name="Home Owner",
        access_level="Manager",
    )
    authorize_account("acct-1", "owner@example.com")
    app.save_utility_connection(
        "acct-1",
        {
            "provider_name": "Duke Energy Carolinas, LLC",
            "connection_label": "Main account",
            "access_method": "customer_api_key",
            "access_identifier": "https://utility.example.test/meter.xml",
            "access_secret": "customer-approved-key-1234",
        },
    )
    client = app.web_app.test_client()
    sign_in(client)

    response = client.post(
        f"/account-access/{access['id']}/delete",
        data={"account_number": "acct-1"},
        follow_redirects=False,
    )
    authorization = app.get_customer_account_data_authorization("acct-1", int(customer["id"]))
    connection = app.list_utility_connections("acct-1")[0]
    with app.get_db_connection() as conn:
        events = conn.execute(
            "SELECT action, metadata_json FROM audit_events WHERE account_id = ? ORDER BY id",
            (app.find_account("acct-1")["id"],),
        ).fetchall()

    assert response.status_code == 302
    assert app.get_customer_account_access("owner@example.com", "acct-1") is None
    assert authorization["status"] == "revoked_access_removed"
    assert connection["access_identifier"] == ""
    assert connection["secret_last4"] is None
    assert connection["status"] == "Authorization withdrawn"
    assert any(row["action"] == "utility.authorization_revoked" for row in events)
    access_event = next(row for row in events if row["action"] == "account.access_revoked")
    assert json.loads(access_event["metadata_json"])["credentials_cleared"] is True


def test_customer_data_export_is_tenant_bounded_and_excludes_secrets(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    app.create_customer_user("other@example.com", "Other Owner", "other-password-123")
    app.save_account_profile("acct-a", display_name="Main house", energy_company="Duke Energy Carolinas, LLC")
    app.save_household_profile(
        "acct-a",
        {"address": "123 Main Street", "zip_code": "28205", "occupant_count": "3"},
    )
    app.add_account_access_email("acct-a", "owner@example.com", full_name="Home Owner", access_level="Manager")
    owner = app.get_customer_user_by_email("owner@example.com")
    with app.get_db_connection() as conn:
        app.record_customer_policy_acceptance(
            conn,
            int(owner["id"]),
            accepted_at="2026-07-21T12:00:00+00:00",
            remote_hash="private-remote-hash",
            user_agent_hash="private-user-agent-hash",
        )
    authorize_account("acct-a", "owner@example.com")
    app.add_load_item("acct-a", label="Kitchen refrigerator", quantity=1, watts_each=150, include_when_off=True)
    app.save_utility_connection(
        "acct-a",
        {
            "provider_name": "Duke Energy Carolinas, LLC",
            "connection_label": "Main account",
            "access_method": "customer_api_key",
            "access_identifier": "owner-utility-login@example.com",
            "access_secret": "customer-approved-secret-1234",
        },
    )
    app.import_interval_file_to_db(FIXTURE, account_number="acct-a", display_name="Main house")
    report_path = tmp_path / "output" / "authorized-report.csv"
    report_path.write_text("date,total_kwh\n2026-01-01,12.3\n", encoding="utf-8")
    app.register_report_artifacts("acct-a", [report_path])

    app.save_account_profile("acct-b", display_name="Other house", energy_company="Other Utility")
    app.save_household_profile("acct-b", {"address": "999 Other Lane", "zip_code": "27601"})
    app.add_account_access_email("acct-b", "other@example.com", full_name="Other Owner", access_level="Manager")
    app.add_load_item("acct-b", label="Private load", quantity=1, watts_each=999, include_when_off=False)
    client = app.web_app.test_client()
    customer_sign_in(client)

    response = client.get("/customer/data-export.zip")
    with zipfile.ZipFile(BytesIO(response.data)) as archive:
        names = archive.namelist()
        extracted = {name: archive.read(name) for name in names}
    combined = b"\n".join(extracted.values())
    manifest = json.loads(extracted["manifest.json"])
    profile_name = next(name for name in names if name.endswith("/profile.json"))
    profile = json.loads(extracted[profile_name])
    interval_name = next(name for name in names if name.endswith("/interval-readings.csv"))
    report_name = next(name for name in names if name.endswith("/reports/authorized-report.csv"))
    with app.get_db_connection() as conn:
        event = conn.execute(
            "SELECT actor_type, metadata_json FROM audit_events WHERE action = 'customer.data_exported' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert response.status_code == 200
    assert response.mimetype == "application/zip"
    assert "no-store" in response.headers["Cache-Control"]
    assert "home-energy-watch-data-" in response.headers["Content-Disposition"]
    assert manifest["customer"]["email"] == "owner@example.com"
    assert manifest["account_count"] == 1
    assert manifest["format_version"] == 3
    assert manifest["policy_acceptances"][0]["terms_version"] == app.CURRENT_TERMS_VERSION
    assert profile["account"]["account_number"] == "acct-a"
    assert profile["household"]["address"] == "123 Main Street"
    assert profile["inventory"][0]["label"] == "Kitchen refrigerator"
    assert profile["utility_connections"][0]["provider_name"] == "Duke Energy Carolinas, LLC"
    assert profile["data_authorizations"][0]["status"] == "active"
    assert profile["data_requests"] == []
    assert b"2024-01-01" in extracted[interval_name]
    assert b"2026-01-01,12.3" in extracted[report_name]
    assert b"acct-b" not in combined
    assert b"999 Other Lane" not in combined
    assert b"Private load" not in combined
    assert b"customer-approved-secret-1234" not in combined
    assert b"owner-utility-login@example.com" not in combined
    assert b"password_hash" not in combined
    assert b"stripe_customer_id" not in combined
    assert b"private-remote-hash" not in combined
    assert b"private-user-agent-hash" not in combined
    assert b"/data/" not in combined
    assert event["actor_type"] == "customer"
    assert json.loads(event["metadata_json"])["account_count"] == 1


def test_customer_data_export_requires_customer_sign_in(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()

    response = client.get("/customer/data-export.zip")

    assert response.status_code == 302
    assert "/customer/login" in response.headers["Location"]


def test_customer_manager_can_request_and_cancel_deletion_but_viewer_cannot(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    manager = app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    app.create_customer_user("viewer@example.com", "Account Viewer", "customer-password-123")
    app.save_account_profile("acct-delete", display_name="Main home")
    app.add_account_access_email("acct-delete", "owner@example.com", access_level="Manager")
    app.add_account_access_email("acct-delete", "viewer@example.com", access_level="Viewer")

    manager_client = app.web_app.test_client()
    customer_sign_in(manager_client)
    page = manager_client.get("/customer/data-requests")
    unconfirmed = manager_client.post(
        "/customer/data-requests",
        data={"account_number": "acct-delete"},
        follow_redirects=True,
    )
    submitted = manager_client.post(
        "/customer/data-requests",
        data={"account_number": "acct-delete", "confirm_deletion_request": "yes"},
        follow_redirects=True,
    )
    requests = app.list_customer_data_requests(int(manager["id"]))
    duplicate = manager_client.post(
        "/customer/data-requests",
        data={"account_number": "acct-delete", "confirm_deletion_request": "yes"},
        follow_redirects=True,
    )

    viewer_client = app.web_app.test_client()
    customer_sign_in(viewer_client, "viewer@example.com")
    viewer_page = viewer_client.get("/customer/data-requests")
    viewer_submit = viewer_client.post(
        "/customer/data-requests",
        data={"account_number": "acct-delete", "confirm_deletion_request": "yes"},
        follow_redirects=True,
    )

    canceled = manager_client.post(
        f"/customer/data-requests/{requests[0]['id']}/cancel",
        follow_redirects=True,
    )
    saved = app.get_account_data_request(int(requests[0]["id"]))
    with app.get_db_connection() as conn:
        actions = {
            row["action"]
            for row in conn.execute(
                "SELECT action FROM audit_events WHERE target_type = 'account_data_request'"
            ).fetchall()
        }

    assert page.status_code == 200
    assert b"Download my data" in page.data
    assert b"Submit request" in page.data
    assert b"Confirm that you want" in unconfirmed.data
    assert b"submitted for review" in submitted.data
    assert len(requests) == 1
    assert requests[0]["status"] == "pending"
    assert b"already open" in duplicate.data
    assert b"A manager can submit this request" in viewer_page.data
    assert b"Manager access" in viewer_submit.data
    assert canceled.status_code == 200
    assert saved["status"] == "canceled"
    assert actions == {"customer.data_deletion_requested", "customer.data_deletion_canceled"}


def test_data_request_review_is_commissioner_only_and_legal_hold_blocks_approval(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    commissioner = app.create_first_staff_user(
        "commission@example.gov",
        "Commissioner One",
        "test-password-123",
    )
    invite = app.invite_staff_user(
        "analyst@example.gov",
        "Analyst One",
        "Analyst",
        invited_by_id=int(commissioner["id"]),
    )
    app.accept_staff_invite(invite["token"], "analyst-password-123")
    customer = app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    app.save_account_profile("acct-hold", display_name="Held account")
    app.add_account_access_email("acct-hold", "owner@example.com", access_level="Manager")
    data_request = app.create_account_deletion_request("acct-hold", int(customer["id"]))

    analyst_client = app.web_app.test_client()
    analyst_client.post(
        "/login",
        data={"email": "analyst@example.gov", "password": "analyst-password-123"},
    )
    analyst_page = analyst_client.get("/data-requests")

    commissioner_client = app.web_app.test_client()
    sign_in(commissioner_client)
    queue = commissioner_client.get("/data-requests")
    hold_response = commissioner_client.post(
        "/data-holds",
        data={
            "account_number": "acct-hold",
            "reason": "Open billing dispute requires preservation.",
        },
        follow_redirects=True,
    )
    blocked = commissioner_client.post(
        f"/data-requests/{data_request['id']}/review",
        data={"decision": "approve", "review_note": "Identity verified."},
        follow_redirects=True,
    )
    active_hold = app.list_account_legal_holds(active_only=True)[0]
    release_response = commissioner_client.post(
        f"/data-holds/{active_hold['id']}/release",
        follow_redirects=True,
    )
    approved = commissioner_client.post(
        f"/data-requests/{data_request['id']}/review",
        data={"decision": "approve", "review_note": "Identity verified."},
        follow_redirects=True,
    )
    execution_paused = commissioner_client.post(
        f"/data-requests/{data_request['id']}/execute",
        data={"password": "test-password-123"},
        follow_redirects=True,
    )
    saved = app.get_account_data_request(int(data_request["id"]))
    with app.get_db_connection() as conn:
        actions = {
            row["action"]
            for row in conn.execute(
                "SELECT action FROM audit_events WHERE action LIKE 'account.legal_hold_%'"
            ).fetchall()
        }

    assert analyst_page.status_code == 302
    assert analyst_page.headers["Location"].endswith("/")
    assert queue.status_code == 200
    assert b"Customer deletion requests" in queue.data
    assert b"Deletion paused" in queue.data
    assert b"The legal hold is active" in hold_response.data
    assert b"Release the legal hold" in blocked.data
    assert b"legal hold was released" in release_response.data
    assert b"approved, awaiting deletion" in approved.data.lower()
    assert b"deletion remains paused" in execution_paused.data.lower()
    assert saved["status"] == "approved"
    assert actions == {"account.legal_hold_placed", "account.legal_hold_released"}


def test_data_deletion_requires_policy_version_when_enabled(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("POWER_DATA_DELETION_ENABLED", "true")

    with pytest.raises(RuntimeError, match="POWER_DATA_DELETION_POLICY_VERSION"):
        app.validate_runtime_security()


def test_approved_data_deletion_removes_customer_data_and_keeps_compliance_record(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("POWER_DATA_DELETION_ENABLED", "true")
    monkeypatch.setenv("POWER_DATA_DELETION_POLICY_VERSION", "retention-2026-07")
    app.web_app.config["TESTING"] = True
    commissioner = app.create_first_staff_user(
        "commission@example.gov",
        "Commissioner One",
        "test-password-123",
    )
    customer = app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    app.save_account_profile(
        "acct-erase",
        display_name="Erase this home",
        energy_company="Duke Energy Progress, LLC",
        baseline_date="2026-01-15",
    )
    app.save_household_profile(
        "acct-erase",
        {
            "address": "123 Private Lane",
            "zip_code": "27601",
            "occupant_count": "2",
            "year_built": "1990",
            "square_footage": "1800",
            "heating_system": "Heat pump",
            "cooling_system": "Central air",
            "water_heater": "Gas",
            "notes": "Private household note",
        },
    )
    app.add_account_access_email("acct-erase", "owner@example.com", access_level="Manager")
    authorization = app.grant_account_data_authorization("acct-erase", int(customer["id"]))
    app.add_load_item(
        "acct-erase",
        label="Kitchen refrigerator",
        quantity=1,
        watts_each=150,
        include_when_off=True,
    )
    app.save_utility_connection(
        "acct-erase",
        {
            "provider_name": "Duke Energy Progress, LLC",
            "connection_label": "Main account",
            "access_method": "customer_api_key",
            "access_identifier": "owner-utility@example.com",
            "access_secret": "customer-approved-secret-1234",
        },
    )
    input_path = app.INPUT_DIR / "customer-history.xml"
    input_path.write_bytes(FIXTURE.read_bytes())
    app.import_interval_file_to_db(input_path, account_number="acct-erase")
    report_path = app.OUTPUT_DIR / "customer-report.csv"
    report_path.write_text("date,total_kwh\n2026-01-01,12.3\n", encoding="utf-8")
    app.register_report_artifacts("acct-erase", [report_path])
    account = app.find_account("acct-erase")
    with app.get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO weather_daily_cache (
                account_id, weather_date, latitude, longitude, timezone,
                location_name, data_json, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(account["id"]),
                "2026-01-01",
                35.77,
                -78.64,
                "America/New_York",
                "Raleigh",
                '{"temperature": 40}',
                app.timestamp_now(),
            ),
        )
    data_request = app.create_account_deletion_request("acct-erase", int(customer["id"]))
    app.review_account_deletion_request(
        int(data_request["id"]),
        int(commissioner["id"]),
        "approve",
        "Identity and ownership verified.",
    )

    commissioner_client = app.web_app.test_client()
    sign_in(commissioner_client)
    wrong_password = commissioner_client.post(
        f"/data-requests/{data_request['id']}/execute",
        data={"password": "wrong-password"},
        follow_redirects=True,
    )
    completed = commissioner_client.post(
        f"/data-requests/{data_request['id']}/execute",
        data={"password": "test-password-123"},
        follow_redirects=True,
    )
    saved = app.get_account_data_request(int(data_request["id"]))
    deleted_account = app.find_account(str(saved["account_number"]))
    with app.get_db_connection() as conn:
        erased_counts = {
            table: int(
                conn.execute(
                    f"SELECT COUNT(*) AS count FROM {table} WHERE account_id = ?",
                    (int(account["id"]),),
                ).fetchone()["count"]
            )
            for table in (
                "imported_files",
                "interval_readings",
                "account_load_items",
                "household_profiles",
                "weather_daily_cache",
                "account_access_emails",
                "utility_connections",
                "report_artifacts",
            )
        }
        retained_authorization = conn.execute(
            """
            SELECT status, remote_hash, user_agent_hash
            FROM account_data_authorizations WHERE id = ?
            """,
            (int(authorization["id"]),),
        ).fetchone()
        completion_event = conn.execute(
            """
            SELECT account_id, metadata_json
            FROM audit_events
            WHERE action = 'customer.data_deletion_completed'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()

    assert b"That password did not work" in wrong_password.data
    assert b"approved account data was deleted" in completed.data
    assert app.find_account("acct-erase") is None
    assert deleted_account["display_name"] == "Deleted account"
    assert deleted_account["energy_company"] == ""
    assert deleted_account["baseline_date"] is None
    assert set(erased_counts.values()) == {0}
    assert retained_authorization["status"] == "revoked_deletion"
    assert retained_authorization["remote_hash"] is None
    assert retained_authorization["user_agent_hash"] is None
    assert saved["status"] == "completed"
    assert saved["policy_version"] == "retention-2026-07"
    assert saved["account_number"].startswith(f"deleted-{account['id']}-")
    assert not input_path.exists()
    assert not report_path.exists()
    assert app.get_customer_account_access("owner@example.com", "acct-erase") is None
    assert int(completion_event["account_id"]) == int(account["id"])
    assert json.loads(completion_event["metadata_json"])["policy_version"] == "retention-2026-07"
    assert app.verify_audit_chain()["valid"] is True


def test_utility_connection_stores_customer_granted_access_without_exposing_secret(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)

    connection = app.save_utility_connection(
        "acct-1",
        {
            "provider_name": "Duke Energy",
            "connection_label": "Main Duke login",
            "access_method": "customer_api_key",
            "access_identifier": "customer@example.com",
            "access_secret": "customer_meter_access_1234",
        },
    )

    assert connection["provider_name"] == "Duke Energy"
    assert connection["access_method"] == "customer_api_key"
    assert connection["access_identifier"] == "customer@example.com"
    assert connection["secret_last4"] == "1234"
    assert "customer_meter_access" not in json.dumps(connection)
    assert app.list_utility_connections("acct-1")[0]["secret_last4"] == "1234"


def test_utility_connection_keeps_retrievable_access_for_sync_without_listing_secret(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("POWER_APP_SECRET", "sync-test-secret")

    connection = app.save_utility_connection(
        "acct-1",
        {
            "provider_name": "Duke Energy",
            "connection_label": "Main Duke login",
            "access_method": "customer_api_key",
            "access_identifier": "https://utility.example.test/meter.xml",
            "access_secret": "customer-approved-key-1234",
        },
    )

    sync_connection = app.load_utility_connection_for_sync("acct-1", int(connection["id"]))

    assert sync_connection["access_secret"] == "customer-approved-key-1234"
    assert "customer-approved-key" not in json.dumps(app.list_utility_connections("acct-1"))
    assert "customer-approved-key" not in json.dumps(connection)


def test_sync_utility_connection_imports_customer_history(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("POWER_APP_SECRET", "sync-test-secret")
    authorize_account("acct-1")

    connection = app.save_utility_connection(
        "acct-1",
        {
            "provider_name": "Duke Energy",
            "connection_label": "Main Duke login",
            "access_method": "customer_api_key",
            "access_identifier": "https://utility.example.test/meter.xml",
            "access_secret": "customer-approved-key-1234",
        },
    )
    calls = {}

    def fake_fetch(connection_for_sync):
        calls["identifier"] = connection_for_sync["access_identifier"]
        calls["secret"] = connection_for_sync["access_secret"]
        return {"filename": "duke-history.xml", "content": FIXTURE.read_bytes()}

    monkeypatch.setattr(app, "fetch_utility_connection_export", fake_fetch)

    result = app.sync_utility_connection("acct-1", int(connection["id"]))

    assert calls == {
        "identifier": "https://utility.example.test/meter.xml",
        "secret": "customer-approved-key-1234",
    }
    assert result["imported"] is True
    assert result["interval_count"] == 6
    assert result["status"] == "Synced"
    assert app.count_imported_files("acct-1") == 1
    assert len(app.load_intervals_from_db("acct-1")) == 6
    assert app.list_utility_connections("acct-1")[0]["last_sync_at"]


def test_run_scheduled_utility_sync_records_success_and_failure(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("POWER_APP_SECRET", "sync-test-secret")
    authorize_account("acct-1")
    authorize_account("acct-2")

    successful = app.save_utility_connection(
        "acct-1",
        {
            "provider_name": "Duke Energy",
            "connection_label": "Main meter",
            "access_method": "customer_api_key",
            "access_identifier": "https://utility.example.test/success.xml",
            "access_secret": "customer-approved-key-1234",
        },
    )
    failed = app.save_utility_connection(
        "acct-2",
        {
            "provider_name": "Duke Energy",
            "connection_label": "Garage meter",
            "access_method": "customer_api_key",
            "access_identifier": "https://utility.example.test/fail.xml",
            "access_secret": "customer-approved-key-5678",
        },
    )

    def fake_fetch(connection_for_sync):
        if connection_for_sync["access_identifier"].endswith("fail.xml"):
            raise RuntimeError("utility export unavailable")
        return {"filename": "duke-history.xml", "content": FIXTURE.read_bytes()}

    monkeypatch.setattr(app, "fetch_utility_connection_export", fake_fetch)

    result = app.run_scheduled_utility_sync()

    assert result["total"] == 2
    assert result["succeeded"] == 1
    assert result["failed"] == 1
    assert [item["success"] for item in result["connections"]] == [True, False]
    assert app.count_imported_files("acct-1") == 1
    assert app.count_imported_files("acct-2") == 0

    successful_status = app.list_utility_connections("acct-1")[0]
    failed_status = app.list_utility_connections("acct-2")[0]
    assert successful_status["id"] == successful["id"]
    assert successful_status["status"] == "Synced"
    assert successful_status["last_sync_status"] == "success"
    assert successful_status["last_sync_error"] is None
    assert successful_status["last_sync_at"]
    assert successful_status["last_sync_attempt_at"]
    assert failed_status["id"] == failed["id"]
    assert failed_status["status"] == "Sync failed"
    assert failed_status["last_sync_status"] == "failed"
    assert failed_status["last_sync_at"] is None
    assert failed_status["last_sync_attempt_at"]
    assert "utility export unavailable" in failed_status["last_sync_error"]


def test_run_scheduled_utility_sync_can_limit_to_one_account(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("POWER_APP_SECRET", "sync-test-secret")
    authorize_account("acct-1")
    authorize_account("acct-2")

    app.save_utility_connection(
        "acct-1",
        {
            "provider_name": "Duke Energy",
            "connection_label": "Main meter",
            "access_method": "customer_api_key",
            "access_identifier": "https://utility.example.test/acct-1.xml",
            "access_secret": "customer-approved-key-1234",
        },
    )
    app.save_utility_connection(
        "acct-2",
        {
            "provider_name": "Duke Energy",
            "connection_label": "Garage meter",
            "access_method": "customer_api_key",
            "access_identifier": "https://utility.example.test/acct-2.xml",
            "access_secret": "customer-approved-key-5678",
        },
    )
    synced_accounts = []

    def fake_fetch(connection_for_sync):
        synced_accounts.append(connection_for_sync["account_number"])
        return {"filename": "duke-history.xml", "content": FIXTURE.read_bytes()}

    monkeypatch.setattr(app, "fetch_utility_connection_export", fake_fetch)

    result = app.run_scheduled_utility_sync(account_number="acct-2")

    assert result["total"] == 1
    assert result["succeeded"] == 1
    assert result["failed"] == 0
    assert synced_accounts == ["acct-2"]
    assert app.list_utility_connections("acct-1")[0]["last_sync_status"] is None
    assert app.list_utility_connections("acct-2")[0]["last_sync_status"] == "success"


def test_customer_can_sync_own_utility_connection(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    monkeypatch.setenv("POWER_APP_SECRET", "sync-test-secret")

    customer = app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    app.save_account_profile("acct-1", display_name="Allowed home")
    app.add_account_access_email("acct-1", "owner@example.com", full_name="Home Owner", access_level="Manager")
    authorize_account("acct-1", "owner@example.com")
    connection = app.save_utility_connection(
        "acct-1",
        {
            "provider_name": "Duke Energy",
            "connection_label": "Main Duke login",
            "access_method": "customer_api_key",
            "access_identifier": "https://utility.example.test/meter.xml",
            "access_secret": "customer-approved-key-1234",
        },
    )

    def fake_fetch(connection_for_sync):
        assert connection_for_sync["access_secret"] == "customer-approved-key-1234"
        return {"filename": "duke-history.xml", "content": FIXTURE.read_bytes()}

    monkeypatch.setattr(app, "fetch_utility_connection_export", fake_fetch)

    client = app.web_app.test_client()
    customer_sign_in(client)
    response = client.post(
        f"/utility-connection/{connection['id']}/sync",
        data={"account_number": "acct-1"},
        follow_redirects=True,
    )

    assert customer["email"] == "owner@example.com"
    assert response.status_code == 200
    assert b"Utility history synced." in response.data
    assert app.count_imported_files("acct-1") == 1


def test_customer_can_withdraw_and_restore_utility_data_permission(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    monkeypatch.setenv("POWER_APP_SECRET", "authorization-test-secret")
    customer = app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    app.save_account_profile(
        "acct-1",
        display_name="Allowed home",
        energy_company="Duke Energy Carolinas, LLC",
    )
    app.add_account_access_email(
        "acct-1",
        "owner@example.com",
        full_name="Home Owner",
        access_level="Manager",
    )
    authorize_account("acct-1", "owner@example.com")
    connection = app.save_utility_connection(
        "acct-1",
        {
            "provider_name": "Duke Energy Carolinas, LLC",
            "connection_label": "Main account",
            "access_method": "customer_api_key",
            "access_identifier": "https://utility.example.test/meter.xml",
            "access_secret": "customer-approved-key-1234",
        },
    )
    client = app.web_app.test_client()
    customer_sign_in(client)

    utility_page = client.get("/customer/utility", query_string={"account_number": "acct-1"})
    revoked = client.post(
        "/account/data-authorization/revoke",
        data={"account_number": "acct-1", "return_to": "/customer/utility?account_number=acct-1"},
        follow_redirects=True,
    )
    connection_after_revoke = app.list_utility_connections("acct-1")[0]
    authorization_after_revoke = app.get_customer_account_data_authorization("acct-1", int(customer["id"]))

    assert utility_page.status_code == 200
    assert b"Customer permission" in utility_page.data
    assert b"Withdraw permission" in utility_page.data
    assert revoked.status_code == 200
    assert b"Saved utility access details were removed" in revoked.data
    assert authorization_after_revoke["status"] == "revoked"
    assert connection_after_revoke["access_identifier"] == ""
    assert connection_after_revoke["secret_last4"] is None
    assert connection_after_revoke["status"] == "Authorization withdrawn"
    with pytest.raises(ValueError, match="Customer authorization is required"):
        app.sync_utility_connection("acct-1", int(connection["id"]))

    restored = client.post(
        "/account/data-authorization",
        data={
            "account_number": "acct-1",
            "confirm_data_authorization": "yes",
            "return_to": "/customer/utility?account_number=acct-1",
        },
        follow_redirects=True,
    )
    authorization_after_restore = app.get_customer_account_data_authorization("acct-1", int(customer["id"]))
    with app.get_db_connection() as conn:
        audit_actions = [
            row["action"]
            for row in conn.execute(
                "SELECT action FROM audit_events WHERE actor_id = ? ORDER BY id",
                (customer["id"],),
            ).fetchall()
        ]

    assert restored.status_code == 200
    assert b"Permission to use this account&#39;s utility data is active" in restored.data
    assert authorization_after_restore["status"] == "active"
    assert "utility.authorization_revoked" in audit_actions
    assert "utility.authorization_granted" in audit_actions


def test_customer_utility_page_does_not_offer_chrome_helper_duke_flow(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True

    app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    app.save_account_profile(
        "acct-1",
        display_name="Allowed home",
        energy_company="Duke Energy Carolinas, LLC",
    )
    app.add_account_access_email("acct-1", "owner@example.com", full_name="Home Owner", access_level="Manager")

    client = app.web_app.test_client()
    customer_sign_in(client)
    response = client.get("/customer/utility", query_string={"account_number": "acct-1"})
    start_response = client.get(
        "/utility-connection/duke/start",
        query_string={"account_number": "acct-1"},
    )
    finish_response = client.post(
        "/utility-connection/duke/complete",
        data={"account_number": "acct-1", "authorization_code": "duke-code-123"},
    )

    assert response.status_code == 200
    assert b"Download your Duke history" in response.data
    assert b"Go to Duke My Account" in response.data
    assert b"Green Button customer connection" in response.data
    assert b"North Carolina data-access track" in response.data
    assert b"Open Duke sign-in" not in response.data
    assert b"Duke sign-in code" not in response.data
    assert start_response.status_code == 404
    assert finish_response.status_code == 404


def test_account_page_filters_by_name_or_address_and_paginates(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)

    for index in range(25):
        account_number = f"acct-{index:02d}"
        app.save_account_profile(account_number, display_name=f"Customer {index:02d}")
        app.save_household_profile(
            account_number,
            {
                "address": f"{index} Oak Street",
                "occupant_count": "",
                "year_built": "",
                "square_footage": "",
                "heating_system": "",
                "cooling_system": "",
                "water_heater": "",
                "notes": "",
            },
        )
    app.save_household_profile(
        "acct-17",
        {
            "address": "17 Pine Commission Road",
            "occupant_count": "",
            "year_built": "",
            "square_footage": "",
            "heating_system": "",
            "cooling_system": "",
            "water_heater": "",
            "notes": "",
        },
    )

    first_page = app.list_account_page(page=1, per_page=10)
    search_page = app.list_account_page(search="pine", page=1, per_page=10)

    assert len(first_page["accounts"]) == 10
    assert first_page["total"] == 26
    assert first_page["has_next"] is True
    assert search_page["total"] == 1
    assert search_page["accounts"][0]["account_number"] == "acct-17"
    assert search_page["accounts"][0]["address"] == "17 Pine Commission Road"


def test_customer_signup_creates_login_and_account_access(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    stub_utility_lookup(monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()

    response = client.post(
        "/signup",
        data={
            "full_name": "Home Owner",
            "email": "owner@example.com",
            "password": "customer-password-123",
            "account_number": "duke-123",
            "address": "123 Main St Charlotte NC",
            "zip_code": "28205",
            "accept_policies": "yes",
            "confirm_account_authority": "yes",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/customer")
    assert app.authenticate_customer_user("owner@example.com", "customer-password-123")["email"] == "owner@example.com"
    account = app.find_account("duke-123")
    assert account["energy_company"] == "Duke Energy Progress, LLC"
    access = app.list_account_access_emails("duke-123")
    assert access[0]["email"] == "owner@example.com"
    assert access[0]["access_level"] == "Manager"
    customer = app.get_customer_user_by_email("owner@example.com")
    authorization = app.get_customer_account_data_authorization("duke-123", int(customer["id"]))
    with app.get_db_connection() as conn:
        policy = conn.execute(
            "SELECT * FROM customer_policy_acceptances WHERE customer_user_id = ?",
            (customer["id"],),
        ).fetchone()
        audit_actions = {
            row["action"]
            for row in conn.execute(
                "SELECT action FROM audit_events WHERE actor_id = ?",
                (customer["id"],),
            ).fetchall()
        }

    assert policy["terms_version"] == app.CURRENT_TERMS_VERSION
    assert policy["privacy_version"] == app.CURRENT_PRIVACY_VERSION
    assert len(policy["remote_hash"]) == 64
    assert len(policy["user_agent_hash"]) == 64
    assert authorization["active"] is True
    assert authorization["authorization_scope"] == app.UTILITY_AUTHORIZATION_SCOPE
    assert {"customer.policy_accepted", "utility.authorization_granted"} <= audit_actions

    dashboard = client.get("/customer")

    assert dashboard.status_code == 200
    assert b"Your energy history" in dashboard.data
    assert b"Billing" in dashboard.data
    assert b"History" in dashboard.data
    assert b"Commission access" not in dashboard.data

    account_page = client.get("/customer/account")
    assert account_page.status_code == 200
    assert b"Duke Energy Progress, LLC" in account_page.data


def test_customer_signup_requires_policy_and_account_authority_confirmations(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    stub_utility_lookup(monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()
    signup_data = {
        "full_name": "Home Owner",
        "email": "owner@example.com",
        "password": "customer-password-123",
        "account_number": "duke-consent",
        "address": "123 Main St Charlotte NC",
        "zip_code": "28205",
    }

    missing_policies = client.post(
        "/signup",
        data={**signup_data, "confirm_account_authority": "yes"},
    )
    missing_authority = client.post(
        "/signup",
        data={**signup_data, "accept_policies": "yes"},
    )
    with app.get_db_connection() as conn:
        customer_count = conn.execute(
            "SELECT COUNT(*) AS count FROM customer_users WHERE email = ?",
            ("owner@example.com",),
        ).fetchone()["count"]

    assert missing_policies.status_code == 200
    assert b"Agree to the Terms and Privacy Notice" in missing_policies.data
    assert missing_authority.status_code == 200
    assert b"Confirm that you are allowed to manage this electric account" in missing_authority.data
    assert int(customer_count) == 0


def test_customer_signup_keeps_entered_values_on_validation_error(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    stub_utility_lookup(monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()

    response = client.post(
        "/signup",
        data={
            "full_name": "Home Owner",
            "email": "owner@example.com",
            "password": "short",
            "account_number": "duke-123",
            "address": "123 Main St Charlotte NC",
            "zip_code": "28205",
            "plan_id": "review",
            "accept_policies": "yes",
            "confirm_account_authority": "yes",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Use at least 10 characters for the password." in response.data
    assert b'value="Home Owner"' in response.data
    assert b'value="owner@example.com"' in response.data
    assert b'value="duke-123"' in response.data
    assert b'value="123 Main St Charlotte NC"' in response.data
    assert b'value="28205"' in response.data
    assert b'value="review"' in response.data
    assert b"checked" in response.data
    assert b'value="short"' not in response.data
    assert b'name="energy_company"' not in response.data


def test_customer_signup_cannot_claim_an_existing_electric_account(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    stub_utility_lookup(monkeypatch)
    app.web_app.config["TESTING"] = True
    app.save_account_profile("duke-registered", display_name="Existing household")
    client = app.web_app.test_client()

    response = client.post(
        "/signup",
        data={
            "full_name": "Unknown Person",
            "email": "unknown@example.com",
            "password": "customer-password-123",
            "account_number": "duke-registered",
            "address": "123 Main St Charlotte NC",
            "zip_code": "28205",
            "accept_policies": "yes",
            "confirm_account_authority": "yes",
        },
        follow_redirects=True,
    )
    with app.get_db_connection() as conn:
        user_count = conn.execute(
            "SELECT COUNT(*) AS count FROM customer_users WHERE email = ?",
            ("unknown@example.com",),
        ).fetchone()["count"]

    assert response.status_code == 200
    assert b"already registered" in response.data
    assert int(user_count) == 0


def test_customer_signup_rolls_back_every_record_when_account_creation_fails(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)

    def fail_account_creation(*args, **kwargs):
        raise RuntimeError("simulated account write failure")

    monkeypatch.setattr(app, "get_or_create_account", fail_account_creation)
    with pytest.raises(RuntimeError, match="simulated account write failure"):
        app.create_customer_signup(
            email="rollback@example.com",
            full_name="Rollback Customer",
            password="customer-password-123",
            account_number="rollback-account",
            energy_company="Duke Energy Progress, LLC",
            plan_id="home",
            household_form={"address": "123 Main Street", "zip_code": "28205"},
            accept_policies=True,
            confirm_account_authority=True,
        )
    with app.get_db_connection() as conn:
        user_count = conn.execute(
            "SELECT COUNT(*) AS count FROM customer_users WHERE email = ?",
            ("rollback@example.com",),
        ).fetchone()["count"]
        account_count = conn.execute(
            "SELECT COUNT(*) AS count FROM accounts WHERE account_number = ?",
            ("rollback-account",),
        ).fetchone()["count"]

    assert int(user_count) == 0
    assert int(account_count) == 0


def test_account_forms_match_energy_company_from_zip_instead_of_listing_providers(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()

    sign_in(client)
    home = client.get("/account")
    signup = client.get("/signup")

    assert home.status_code == 200
    assert signup.status_code == 200
    assert b"Your electric company" in home.data
    assert b"Your electric company" in signup.data
    assert b"Service ZIP code" in home.data
    assert b"Service ZIP code" in signup.data
    assert b'name="energy_company"' not in home.data
    assert b'name="energy_company"' not in signup.data
    assert b"ENERGYUNITED EMC" not in home.data
    assert b"Home name" not in home.data
    assert b"Home name" not in signup.data


def test_account_profile_stores_and_searches_energy_company(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)

    account = app.save_account_profile(
        "acct-1",
        energy_company="Duke Energy Progress, LLC",
        baseline_date="2024-01-02",
    )
    page = app.list_account_page(search="progress")

    assert account["energy_company"] == "Duke Energy Progress, LLC"
    assert page["total"] == 1
    assert page["accounts"][0]["account_number"] == "acct-1"


def test_account_save_matches_provider_from_service_zip(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    stub_utility_lookup(monkeypatch, company="Duke Energy Carolinas, LLC")
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()
    sign_in(client)

    response = client.post(
        "/account",
        data={
            "account_number": "acct-new",
            "address": "123 Main Street, Charlotte, NC",
            "zip_code": "28205",
            "energy_company": "Town of Apex",
            "baseline_date": "",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert app.find_account("acct-new")["energy_company"] == "Duke Energy Carolinas, LLC"
    assert app.load_household_profile("acct-new")["zip_code"] == "28205"


def test_history_import_keeps_zip_matched_provider_when_request_is_tampered(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    authorize_account("acct-1")
    app.save_account_profile("acct-1", energy_company="Duke Energy Progress, LLC")
    app.save_household_profile("acct-1", {"address": "1 E Edenton St", "zip_code": "27601"})
    client = app.web_app.test_client()
    sign_in(client)

    response = client.post(
        "/analyze",
        data={
            "account_number": "acct-1",
            "energy_company": "Town of Apex",
            "xml_file": (BytesIO(FIXTURE.read_bytes()), "history.xml"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert app.find_account("acct-1")["energy_company"] == "Duke Energy Progress, LLC"


def test_api_history_import_keeps_zip_matched_provider_when_request_is_tampered(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    authorize_account("acct-1")
    app.save_account_profile("acct-1", energy_company="Duke Energy Progress, LLC")
    app.save_household_profile("acct-1", {"address": "1 E Edenton St", "zip_code": "27601"})
    input_path = app.INPUT_DIR / "history.xml"
    input_path.write_bytes(FIXTURE.read_bytes())
    client = app.web_app.test_client()
    sign_in(client)

    response = client.post(
        "/api/analyze",
        json={
            "account_number": "acct-1",
            "energy_company": "Town of Apex",
            "input_file": input_path.name,
        },
    )

    assert response.status_code == 200
    assert app.find_account("acct-1")["energy_company"] == "Duke Energy Progress, LLC"


def test_utility_connection_uses_account_provider_instead_of_submitted_provider(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    app.save_account_profile("acct-1", energy_company="Duke Energy Progress, LLC")
    app.save_household_profile("acct-1", {"address": "1 Main Street", "zip_code": "27601"})
    authorize_account("acct-1")
    client = app.web_app.test_client()
    sign_in(client)

    response = client.post(
        "/utility-connection",
        data={
            "account_number": "acct-1",
            "provider_name": "Town of Apex",
            "connection_label": "Main account",
            "access_method": "utility_authorization",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert app.list_utility_connections("acct-1")[0]["provider_name"] == "Duke Energy Progress, LLC"


def test_staff_cannot_save_utility_connection_without_customer_permission(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    app.save_account_profile("acct-1", energy_company="Duke Energy Progress, LLC")
    app.save_household_profile("acct-1", {"address": "1 Main Street", "zip_code": "27601"})
    client = app.web_app.test_client()
    sign_in(client)

    response = client.post(
        "/utility-connection",
        data={
            "account_number": "acct-1",
            "connection_label": "Main account",
            "access_method": "utility_authorization",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Customer authorization is required" in response.data
    assert app.list_utility_connections("acct-1") == []


def test_public_utility_lookup_endpoint_returns_matched_company(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    stub_utility_lookup(monkeypatch, company="Duke Energy Carolinas, LLC")
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()

    response = client.get(
        "/api/utility-by-zip",
        query_string={"zip_code": "28205", "address": "123 Main Street"},
    )

    assert response.status_code == 200
    assert response.get_json()["energy_company"] == "Duke Energy Carolinas, LLC"


def test_setup_sections_are_separate_menu_pages(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()

    sign_in(client)
    review = client.get("/")

    assert review.status_code == 200
    assert b"Review" in review.data
    assert b"Customer" in review.data
    assert b"People" in review.data
    assert b"Utility" in review.data
    assert b"Inventory" in review.data
    assert b"History" in review.data
    assert b"Staff" in review.data
    assert b"Commission access" not in review.data
    assert b"People with account access" not in review.data
    assert b"Utility data connection" not in review.data
    assert b"House load list" not in review.data
    assert b"Files that work today" not in review.data

    pages = {
        "/staff": b"Commission access",
        "/account": b"Energy company",
        "/people": b"People with account access",
        "/utility": b"Utility data connection",
        "/inventory": b"House load list",
        "/history": b"Files that work today",
    }
    for path, expected_text in pages.items():
        response = client.get(path)
        assert response.status_code == 200
        assert expected_text in response.data


def test_customer_account_page_only_lists_accessible_accounts_and_filters(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True

    app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    for index in range(25):
        account_number = f"acct-{index:02d}"
        app.save_account_profile(account_number, display_name=f"Customer {index:02d}")
        app.save_household_profile(
            account_number,
            {
                "address": f"{index} Oak Street",
                "occupant_count": "",
                "year_built": "",
                "square_footage": "",
                "heating_system": "",
                "cooling_system": "",
                "water_heater": "",
                "notes": "",
            },
        )
        if index < 18:
            app.add_account_access_email(account_number, "owner@example.com")
    app.save_household_profile(
        "acct-17",
        {
            "address": "17 Pine Commission Road",
            "occupant_count": "",
            "year_built": "",
            "square_footage": "",
            "heating_system": "",
            "cooling_system": "",
            "water_heater": "",
            "notes": "",
        },
    )

    page = app.list_customer_account_page("owner@example.com", page=1, per_page=10)
    search_page = app.list_customer_account_page("owner@example.com", search="pine", page=1, per_page=10)

    assert page["total"] == 18
    assert len(page["accounts"]) == 10
    assert page["has_next"] is True
    assert search_page["total"] == 1
    assert search_page["accounts"][0]["account_number"] == "acct-17"

    client = app.web_app.test_client()
    customer_sign_in(client)
    dashboard = client.get("/customer/account", query_string={"account_search": "pine"})

    assert dashboard.status_code == 200
    assert b"17 Pine Commission Road" in dashboard.data
    assert b"24 Oak Street" not in dashboard.data


def test_customer_cannot_access_unshared_account_data(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True

    app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    app.save_account_profile("acct-1", display_name="Allowed home")
    app.save_account_profile("acct-2", display_name="Other home")
    app.add_account_access_email("acct-1", "owner@example.com")

    client = app.web_app.test_client()
    customer_sign_in(client)
    response = client.get(
        "/api/day-detail",
        query_string={"account_number": "acct-2", "date": "2024-01-01"},
    )

    assert response.status_code == 403


def test_viewer_can_read_account_but_cannot_change_it(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    app.create_customer_user("viewer@example.com", "Account Viewer", "customer-password-123")
    app.save_account_profile("acct-view", display_name="View-only home", energy_company="Duke Energy Progress, LLC")
    app.save_household_profile("acct-view", {"address": "1 Read Only Lane", "zip_code": "27601"})
    app.add_account_access_email("acct-view", "viewer@example.com", access_level="Viewer")
    client = app.web_app.test_client()
    customer_sign_in(client, "viewer@example.com")

    page = client.get("/customer/account", query_string={"account_number": "acct-view"})
    account_change = client.post(
        "/account",
        data={
            "account_number": "acct-view",
            "address": "2 Changed Lane",
            "zip_code": "27601",
        },
    )
    inventory_change = client.post(
        "/load-items",
        data={
            "account_number": "acct-view",
            "label": "Unauthorized load",
            "quantity": "1",
            "watts_each": "100",
        },
    )

    assert page.status_code == 200
    assert b"view-only access" in page.data
    assert b"disabled" in page.data
    assert account_change.status_code == 403
    assert inventory_change.status_code == 403
    assert app.load_household_profile("acct-view")["address"] == "1 Read Only Lane"
    assert app.list_load_items("acct-view") == []


def test_billing_plans_do_not_publish_unapproved_prices(monkeypatch):
    monkeypatch.delenv("POWER_BILLING_ENABLED", raising=False)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_stale")
    monkeypatch.setenv("STRIPE_PRICE_HOME", "price_stale_home")
    monkeypatch.setenv("STRIPE_PRICE_REVIEW", "price_stale_review")
    plans = app.list_billing_plans()
    home = next(plan for plan in plans if plan["id"] == "home")
    review = next(plan for plan in plans if plan["id"] == "review")
    agency = next(plan for plan in plans if plan["id"] == "agency")

    assert home["monthly_price_label"] == "Pricing being finalized"
    assert home["account_limit"] == 1
    assert home["payment_ready"] is False
    assert review["monthly_price_label"] == "Pricing being finalized"
    assert review["account_limit"] == 20
    assert review["payment_ready"] is False
    assert agency["monthly_price_label"] == "Talk with us"
    assert agency["payment_ready"] is False


def test_customer_signup_saves_selected_billing_plan(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    stub_utility_lookup(monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()

    response = client.post(
        "/signup",
        data={
            "full_name": "Home Owner",
            "email": "owner@example.com",
            "password": "customer-password-123",
            "account_number": "duke-123",
            "display_name": "Main house",
            "address": "123 Main St Charlotte NC",
            "zip_code": "28205",
            "plan_id": "review",
            "accept_policies": "yes",
            "confirm_account_authority": "yes",
        },
        follow_redirects=False,
    )

    customer = app.authenticate_customer_user("owner@example.com", "customer-password-123")
    billing = app.load_customer_billing(int(customer["id"]))

    assert response.status_code == 302
    assert billing["plan_id"] == "review"
    assert billing["status"] == "not_started"

    dashboard = client.get("/customer")
    assert b"Billing" in dashboard.data
    billing_page = client.get("/customer/billing")
    assert b"Online billing is not open yet" in billing_page.data


class FakeStripeCheckoutSession:
    calls = []
    retrieve_result = None

    @classmethod
    def create(cls, **kwargs):
        cls.calls.append(kwargs)
        return {
            "id": "cs_test_123",
            "url": "https://checkout.stripe.com/c/cs_test_123",
            "customer": "cus_123",
            "subscription": "sub_123",
        }

    @classmethod
    def retrieve(cls, session_id, expand=None):
        cls.calls.append({"retrieve": session_id, "expand": expand})
        return cls.retrieve_result or {
            "id": session_id,
            "customer": "cus_123",
            "subscription": {
                "id": "sub_123",
                "status": "active",
                "current_period_end": 1784505600,
            },
            "payment_intent": {"id": "pi_123", "charges": {"data": [{"receipt_url": "https://pay.stripe.com/receipts/test"}]}},
            "invoice": {"hosted_invoice_url": "https://pay.stripe.com/invoice/test"},
            "payment_status": "paid",
            "metadata": {"customer_user_id": "1", "plan_id": "home"},
        }


class FakeStripeBillingPortalSession:
    calls = []

    @classmethod
    def create(cls, **kwargs):
        cls.calls.append(kwargs)
        return {"url": "https://billing.stripe.com/p/session"}


class FakeStripeWebhook:
    event = None
    calls = []

    @classmethod
    def construct_event(cls, payload, signature, secret):
        cls.calls.append({"payload": payload, "signature": signature, "secret": secret})
        return cls.event


class FakeStripeCheckout:
    Session = FakeStripeCheckoutSession


class FakeStripeBillingPortal:
    Session = FakeStripeBillingPortalSession


class FakeStripe:
    checkout = FakeStripeCheckout
    billing_portal = FakeStripeBillingPortal
    Webhook = FakeStripeWebhook
    api_key = None
    api_version = None


def install_fake_stripe(monkeypatch):
    FakeStripeCheckoutSession.calls = []
    FakeStripeCheckoutSession.retrieve_result = None
    FakeStripeBillingPortalSession.calls = []
    FakeStripeWebhook.calls = []
    FakeStripeWebhook.event = None
    FakeStripe.api_key = None
    FakeStripe.api_version = None
    monkeypatch.setattr(app, "stripe", FakeStripe)
    monkeypatch.setenv("POWER_BILLING_ENABLED", "true")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_home_energy_watch")
    monkeypatch.setenv("STRIPE_PRICE_HOME", "price_home_123")
    monkeypatch.setenv("STRIPE_PRICE_REVIEW", "price_review_456")
    monkeypatch.setenv("STRIPE_API_VERSION", "2026-02-25.clover")


def test_customer_signup_paid_plan_redirects_to_stripe_checkout(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    stub_utility_lookup(monkeypatch, company="Duke Energy Carolinas, LLC")
    app.web_app.config["TESTING"] = True
    install_fake_stripe(monkeypatch)
    monkeypatch.setenv("POWER_PUBLIC_BASE_URL", "https://app.homeenergywatch.com")

    client = app.web_app.test_client()
    response = client.post(
        "/signup",
        data={
            "full_name": "Home Owner",
            "email": "owner@example.com",
            "password": "customer-password-123",
            "account_number": "duke-123",
            "address": "123 Main St Charlotte NC",
            "zip_code": "28205",
            "plan_id": "home",
            "accept_policies": "yes",
            "confirm_account_authority": "yes",
        },
        follow_redirects=False,
    )
    customer = app.authenticate_customer_user("owner@example.com", "customer-password-123")
    billing = app.load_customer_billing(int(customer["id"]))
    checkout_call = FakeStripeCheckoutSession.calls[0]

    assert response.status_code == 302
    assert response.headers["Location"] == "https://checkout.stripe.com/c/cs_test_123"
    assert checkout_call["mode"] == "subscription"
    assert checkout_call["line_items"] == [{"price": "price_home_123", "quantity": 1}]
    assert checkout_call["success_url"] == "https://app.homeenergywatch.com/billing/success?session_id={CHECKOUT_SESSION_ID}"
    assert checkout_call["cancel_url"] == "https://app.homeenergywatch.com/billing/cancel"
    assert checkout_call["metadata"]["project"] == "home-energy-watch"
    assert checkout_call["metadata"]["customer_user_id"] == str(customer["id"])
    assert checkout_call["metadata"]["plan_id"] == "home"
    assert billing["status"] == "checkout_started"
    assert billing["checkout_session_id"] == "cs_test_123"
    assert billing["stripe_customer_id"] == "cus_123"
    assert billing["stripe_subscription_id"] == "sub_123"


def test_customer_checkout_redirects_to_stripe_checkout_session(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    install_fake_stripe(monkeypatch)
    monkeypatch.setenv("POWER_PUBLIC_BASE_URL", "https://app.homeenergywatch.com")
    customer = app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    app.record_customer_plan_selection(int(customer["id"]), "review")

    client = app.web_app.test_client()
    customer_sign_in(client)
    response = client.post("/billing/checkout", data={"plan_id": "review"}, follow_redirects=False)
    billing = app.load_customer_billing(int(customer["id"]))
    checkout_call = FakeStripeCheckoutSession.calls[0]

    assert response.status_code == 302
    assert response.headers["Location"] == "https://checkout.stripe.com/c/cs_test_123"
    assert checkout_call["line_items"] == [{"price": "price_review_456", "quantity": 1}]
    assert checkout_call["subscription_data"]["metadata"]["project_name"] == "Home Energy Watch"
    assert billing["checkout_session_id"] == "cs_test_123"
    assert billing["stripe_customer_id"] == "cus_123"
    assert billing["stripe_subscription_id"] == "sub_123"
    assert billing["status"] == "checkout_started"


def test_customer_checkout_stays_closed_without_pricing_approval(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    install_fake_stripe(monkeypatch)
    monkeypatch.setenv("POWER_BILLING_ENABLED", "false")
    customer = app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")

    with pytest.raises(ValueError, match="Online payment is not open yet"):
        app.create_customer_checkout_session(customer, "home", "https://app.homeenergywatch.com")
    assert FakeStripeCheckoutSession.calls == []


def test_stripe_checkout_requires_secret_key(monkeypatch):
    monkeypatch.setattr(app, "stripe", FakeStripe)
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    try:
        app.configure_stripe()
    except ValueError as exc:
        assert str(exc) == "Payment is not connected yet."
    else:
        raise AssertionError("Stripe checkout was accepted without a backend secret key")


def test_stripe_webhook_updates_customer_subscription(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    install_fake_stripe(monkeypatch)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_home_energy_watch")
    customer = app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    app.record_customer_plan_selection(int(customer["id"]), "home")
    FakeStripeWebhook.event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_123",
                "customer": "cus_123",
                "subscription": "sub_123",
                "payment_status": "paid",
                "metadata": {"customer_user_id": str(customer["id"]), "plan_id": "home"},
            }
        },
    }

    client = app.web_app.test_client()
    response = client.post(
        "/stripe/webhook",
        data=b'{"id":"evt_123"}',
        headers={"Stripe-Signature": "t=123,v1=test"},
    )
    billing = app.load_customer_billing(int(customer["id"]))

    assert response.status_code == 200
    assert FakeStripeWebhook.calls[0]["secret"] == "whsec_test_home_energy_watch"
    assert billing["stripe_customer_id"] == "cus_123"
    assert billing["stripe_subscription_id"] == "sub_123"
    assert billing["checkout_session_id"] == "cs_test_123"
    assert billing["status"] == "active"


def test_billing_success_refreshes_stripe_receipt(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    install_fake_stripe(monkeypatch)
    customer = app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    app.upsert_customer_billing(int(customer["id"]), "home", "checkout_started", checkout_session_id="cs_test_123")

    client = app.web_app.test_client()
    customer_sign_in(client)
    response = client.get("/billing/success", query_string={"session_id": "cs_test_123"}, follow_redirects=False)
    billing = app.load_customer_billing(int(customer["id"]))

    assert response.status_code == 302
    assert FakeStripeCheckoutSession.calls[0]["retrieve"] == "cs_test_123"
    assert billing["status"] == "active"
    assert billing["stripe_payment_intent_id"] == "pi_123"
    assert billing["stripe_receipt_url"] == "https://pay.stripe.com/invoice/test"


def test_first_run_bootstraps_commission_access(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()

    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/first-run")

    setup = client.post(
        "/first-run",
        data={
            "full_name": "Commissioner One",
            "email": "commission@example.gov",
            "password": "test-password-123",
        },
        follow_redirects=False,
    )

    assert setup.status_code == 302
    assert app.count_staff_users() == 1


def test_marketing_host_renders_public_homepage(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()

    response = client.get("/", base_url="https://homeenergywatch.com")

    assert response.status_code == 200
    assert b"See the pattern. Spot what matters." in response.data
    assert b"Watch the story change without losing context." in response.data
    assert b"https://app.homeenergywatch.com/signup" in response.data
    assert b"Commission Sign In" not in response.data


def test_marketing_host_redirects_app_routes_to_app_subdomain(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()

    response = client.get("/login", base_url="https://homeenergywatch.com", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"] == "https://app.homeenergywatch.com/login"


def test_marketing_host_exposes_indexable_robots_and_sitemap(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()

    robots = client.get("/robots.txt", base_url="https://homeenergywatch.com")
    sitemap = client.get("/sitemap.xml", base_url="https://homeenergywatch.com")
    app_robots = client.get("/robots.txt", base_url="https://app.homeenergywatch.com")

    assert robots.status_code == 200
    assert b"Allow: /" in robots.data
    assert b"Sitemap: https://homeenergywatch.com/sitemap.xml" in robots.data
    assert sitemap.status_code == 200
    assert b"https://homeenergywatch.com/for-commissions" in sitemap.data
    assert b"https://homeenergywatch.com/terms" in sitemap.data
    assert b"https://homeenergywatch.com/privacy" in sitemap.data
    assert b"https://homeenergywatch.com/utility-data-authorization" in sitemap.data
    assert b"Disallow: /" in app_robots.data


def test_public_policy_pages_explain_terms_privacy_and_data_permission(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()

    terms = client.get("/terms", base_url="https://homeenergywatch.com")
    privacy = client.get("/privacy", base_url="https://homeenergywatch.com")
    permission = client.get("/utility-data-authorization", base_url="https://homeenergywatch.com")

    assert terms.status_code == 200
    assert b"A flag is a reason to look more closely" in terms.data
    assert privacy.status_code == 200
    assert b"We do not sell household energy history" in privacy.data
    assert permission.status_code == 200
    assert app.UTILITY_AUTHORIZATION_SCOPE.encode() in permission.data
    assert b"cannot create it for the customer" in permission.data


def test_marketing_pricing_page_uses_public_copy(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()

    response = client.get("/pricing", base_url="https://homeenergywatch.com")

    assert response.status_code == 200
    assert b"Plans that match the size of the review." in response.data
    assert b"Home Watch" in response.data
    assert b"Review Desk" in response.data
    assert b"Pricing being finalized" in response.data
    assert b"$19" not in response.data
    assert b"$99" not in response.data


def test_health_endpoint_only_returns_public_status(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()

    response = client.get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_production_security_configuration_fails_closed(monkeypatch):
    encryption_key = base64.urlsafe_b64encode(b"production-encryption-key-000001").decode("ascii")
    monkeypatch.setenv("POWER_ENV", "production")
    monkeypatch.setenv("POWER_PUBLIC_BASE_URL", "https://app.homeenergywatch.com")
    monkeypatch.setenv(
        "POWER_DATABASE_URL",
        "postgresql://test:secret@database.example.test:5432/home_energy_watch?sslmode=require",
    )
    monkeypatch.setenv("POWER_DATA_ENCRYPTION_KEY", encryption_key)

    monkeypatch.setenv("POWER_APP_SECRET", app.DEFAULT_APP_SECRET)
    with pytest.raises(RuntimeError, match="POWER_APP_SECRET"):
        app.validate_runtime_security()

    monkeypatch.setenv("POWER_APP_SECRET", "production-app-secret-value-00001")
    with pytest.raises(RuntimeError, match="POWER_AUDIT_SIGNING_KEY"):
        app.validate_runtime_security()

    monkeypatch.setenv("POWER_AUDIT_SIGNING_KEY", "production-audit-signing-key-00001")
    monkeypatch.setenv("POWER_PUBLIC_BASE_URL", "http://app.homeenergywatch.com")
    with pytest.raises(RuntimeError, match="HTTPS"):
        app.validate_runtime_security()

    monkeypatch.setenv("POWER_PUBLIC_BASE_URL", "https://app.homeenergywatch.com")
    monkeypatch.delenv("POWER_DATA_ENCRYPTION_KEY")
    with pytest.raises(RuntimeError, match="POWER_DATA_ENCRYPTION_KEY"):
        app.validate_runtime_security()

    monkeypatch.setenv("POWER_DATA_ENCRYPTION_KEY", encryption_key)
    with pytest.raises(RuntimeError, match="POWER_EMAIL_BACKEND"):
        app.validate_runtime_security()

    monkeypatch.setenv("POWER_EMAIL_BACKEND", "ses")
    monkeypatch.setenv("POWER_EMAIL_FROM", "support@homeenergywatch.com")
    app.validate_runtime_security()


def test_production_security_requires_postgres_tls(monkeypatch):
    monkeypatch.setenv("POWER_ENV", "production")
    monkeypatch.setenv("POWER_APP_SECRET", "production-app-secret-value-00001")
    monkeypatch.setenv("POWER_AUDIT_SIGNING_KEY", "production-audit-signing-key-00001")
    monkeypatch.setenv("POWER_PUBLIC_BASE_URL", "https://app.homeenergywatch.com")
    monkeypatch.setenv(
        "POWER_DATA_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(b"production-encryption-key-000001").decode("ascii"),
    )
    monkeypatch.setenv("POWER_EMAIL_BACKEND", "ses")
    monkeypatch.setenv("POWER_EMAIL_FROM", "support@homeenergywatch.com")

    monkeypatch.setenv("POWER_DATABASE_URL", "sqlite:///home-energy-watch.db")
    with pytest.raises(RuntimeError, match="Postgres"):
        app.validate_runtime_security()

    monkeypatch.setenv(
        "POWER_DATABASE_URL",
        "postgresql://test:secret@database.example.test:5432/home_energy_watch",
    )
    with pytest.raises(RuntimeError, match="require TLS"):
        app.validate_runtime_security()

    monkeypatch.setenv(
        "POWER_DATABASE_URL",
        "postgresql://test:secret@database.example.test:5432/home_energy_watch?sslmode=verify-full",
    )
    app.validate_runtime_security()


def test_customer_email_confirmation_is_required_hashed_and_single_use(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    stub_utility_lookup(monkeypatch)
    monkeypatch.setenv("POWER_EMAIL_BACKEND", "memory")
    monkeypatch.setenv("POWER_PUBLIC_BASE_URL", "https://app.homeenergywatch.com")
    app.EMAIL_OUTBOX.clear()
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()

    response = client.post(
        "/signup",
        data={
            "full_name": "Home Owner",
            "email": "owner@example.com",
            "password": "customer-password-123",
            "account_number": "duke-verified",
            "address": "123 Main St Charlotte NC",
            "zip_code": "28205",
            "plan_id": "agency",
            "accept_policies": "yes",
            "confirm_account_authority": "yes",
        },
    )
    customer = app.get_customer_user_by_email("owner@example.com")
    token = token_from_latest_email()
    with app.get_db_connection() as conn:
        saved_token = conn.execute(
            "SELECT token_hash, consumed_at FROM customer_auth_tokens WHERE purpose = 'verify_email'"
        ).fetchone()

    assert response.status_code == 200
    assert b"Check your email" in response.data
    assert customer["email_verified"] is False
    with pytest.raises(app.EmailVerificationRequired):
        app.authenticate_customer_user("owner@example.com", "customer-password-123")
    assert saved_token["token_hash"] != token
    assert token not in saved_token["token_hash"]
    assert saved_token["consumed_at"] is None

    confirmation_page = client.get("/customer/verify-email", query_string={"token": token})
    with app.get_db_connection() as conn:
        before_confirmation = conn.execute(
            "SELECT consumed_at FROM customer_auth_tokens WHERE token_hash = ?",
            (app.customer_auth_token_hash(token),),
        ).fetchone()
    confirmation = client.post("/customer/verify-email", data={"token": token})
    replay = client.post("/customer/verify-email", data={"token": token})
    verified_customer = app.get_customer_user_by_email("owner@example.com")

    assert confirmation_page.status_code == 200
    assert before_confirmation["consumed_at"] is None
    assert confirmation.status_code == 302
    assert confirmation.headers["Location"].endswith("/customer")
    assert replay.status_code == 302
    assert replay.headers["Location"].endswith("/customer/verification-sent")
    assert verified_customer["email_verified"] is True
    assert app.authenticate_customer_user("owner@example.com", "customer-password-123")["email_verified"] is True


def test_resending_confirmation_revokes_the_previous_link(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    stub_utility_lookup(monkeypatch)
    monkeypatch.setenv("POWER_EMAIL_BACKEND", "memory")
    monkeypatch.setenv("POWER_PUBLIC_BASE_URL", "https://app.homeenergywatch.com")
    app.EMAIL_OUTBOX.clear()
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()

    client.post(
        "/signup",
        data={
            "full_name": "Home Owner",
            "email": "owner@example.com",
            "password": "customer-password-123",
            "account_number": "duke-resend",
            "address": "123 Main St Charlotte NC",
            "zip_code": "28205",
            "plan_id": "agency",
            "accept_policies": "yes",
            "confirm_account_authority": "yes",
        },
    )
    original_token = token_from_latest_email()
    resend = client.post("/customer/verification/resend", data={"email": "owner@example.com"})
    replacement_token = token_from_latest_email()

    assert resend.status_code == 200
    assert replacement_token != original_token
    assert app.load_valid_customer_auth_token(original_token, "verify_email") is None
    assert app.load_valid_customer_auth_token(replacement_token, "verify_email") is not None


def test_password_reset_is_private_single_use_and_revokes_old_sessions(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    app.EMAIL_OUTBOX.clear()
    customer = app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    app.save_account_profile("duke-reset", display_name="Owner account")
    app.add_account_access_email("duke-reset", "owner@example.com", access_level="Manager")
    monkeypatch.setenv("POWER_EMAIL_BACKEND", "memory")
    monkeypatch.setenv("POWER_PUBLIC_BASE_URL", "https://app.homeenergywatch.com")

    existing_session = app.web_app.test_client()
    customer_sign_in(existing_session)
    reset_client = app.web_app.test_client()
    known = reset_client.post("/customer/forgot-password", data={"email": "owner@example.com"})
    token = token_from_latest_email()
    with app.get_db_connection() as conn:
        saved_token = conn.execute(
            "SELECT token_hash FROM customer_auth_tokens WHERE purpose = 'password_reset'"
        ).fetchone()["token_hash"]
    unknown = app.web_app.test_client().post(
        "/customer/forgot-password",
        data={"email": "nobody@example.com"},
    )

    assert known.status_code == 200
    assert unknown.status_code == 200
    assert b"If an account uses that address" in known.data
    assert b"If an account uses that address" in unknown.data
    assert b"owner@example.com" not in known.data
    assert b"nobody@example.com" not in unknown.data
    assert saved_token != token
    assert token not in saved_token

    reset = reset_client.post(
        "/customer/reset-password",
        data={
            "token": token,
            "password": "new-customer-password-456",
            "password_confirm": "new-customer-password-456",
        },
    )
    replay = reset_client.post(
        "/customer/reset-password",
        data={
            "token": token,
            "password": "another-password-789",
            "password_confirm": "another-password-789",
        },
    )

    assert reset.status_code == 302
    assert reset.headers["Location"].endswith("/customer/login")
    assert replay.status_code == 302
    assert replay.headers["Location"].endswith("/customer/forgot-password")
    with pytest.raises(ValueError):
        app.authenticate_customer_user("owner@example.com", "customer-password-123")
    assert app.authenticate_customer_user("owner@example.com", "new-customer-password-456")["id"] == customer["id"]
    stale_session = existing_session.get("/customer")
    assert stale_session.status_code == 302
    assert "/customer/login" in stale_session.headers["Location"]


def test_customer_auth_link_request_rate_limit_is_persistent(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    monkeypatch.setenv("POWER_EMAIL_BACKEND", "memory")
    app.EMAIL_OUTBOX.clear()
    client = app.web_app.test_client()

    responses = [
        client.post("/customer/forgot-password", data={"email": "owner@example.com"})
        for _ in range(app.AUTH_RATE_LIMIT_MAX_ATTEMPTS + 1)
    ]

    assert all(response.status_code == 200 for response in responses[:-1])
    assert responses[-1].status_code == 429
    with app.get_db_connection() as conn:
        saved_limit = conn.execute(
            "SELECT identity_hash, attempt_count FROM auth_rate_limits WHERE scope = ?",
            ("customer_password_reset",),
        ).fetchone()
    assert int(saved_limit["attempt_count"]) == app.AUTH_RATE_LIMIT_MAX_ATTEMPTS
    assert "owner@example.com" not in saved_limit["identity_hash"]


def test_staff_invites_are_hashed_and_single_use(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    commissioner = app.create_first_staff_user(
        "commission@example.gov",
        "Commissioner One",
        "test-password-123",
    )

    invite = app.invite_staff_user(
        "analyst@example.gov",
        "Analyst One",
        "Analyst",
        invited_by_id=int(commissioner["id"]),
    )
    with app.get_db_connection() as conn:
        saved = conn.execute(
            "SELECT invite_token, invite_token_hash FROM staff_users WHERE email = ?",
            ("analyst@example.gov",),
        ).fetchone()

    assert saved["invite_token"] is None
    assert saved["invite_token_hash"] != invite["token"]
    assert invite["token"] not in saved["invite_token_hash"]
    assert app.load_invited_staff_user(invite["token"])["email"] == "analyst@example.gov"

    accepted = app.accept_staff_invite(invite["token"], "analyst-password-123")
    assert accepted["invite_pending"] is False
    with pytest.raises(ValueError, match="no longer available"):
        app.accept_staff_invite(invite["token"], "another-password-456")


def test_staff_invitation_is_emailed_without_browser_token(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("POWER_EMAIL_BACKEND", "memory")
    monkeypatch.setenv("POWER_PUBLIC_BASE_URL", "https://app.homeenergywatch.com")
    app.EMAIL_OUTBOX.clear()
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()
    sign_in(client)

    response = client.post(
        "/staff/invite",
        data={
            "full_name": "Analyst Two",
            "email": "analyst@example.gov",
            "role": "Analyst",
        },
        follow_redirects=True,
    )
    with client.session_transaction() as browser_session:
        session_invite = browser_session.get("latest_invite_token")
    with app.get_db_connection() as conn:
        saved = conn.execute(
            "SELECT invite_token, invite_token_hash FROM staff_users WHERE email = ?",
            ("analyst@example.gov",),
        ).fetchone()
        audit_event = conn.execute(
            "SELECT metadata_json FROM audit_events WHERE action = 'staff.invited' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert response.status_code == 200
    assert b"An invitation was sent" in response.data
    assert b"Latest setup link" not in response.data
    assert session_invite is None
    assert len(app.EMAIL_OUTBOX) == 1
    assert "https://app.homeenergywatch.com/staff/setup/" in app.EMAIL_OUTBOX[0]["text_body"]
    assert saved["invite_token"] is None
    assert saved["invite_token_hash"]
    assert json.loads(audit_event["metadata_json"])["delivery"] == "email"


def test_staff_password_reset_is_private_single_use_and_revokes_sessions(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    app.create_first_staff_user(
        "commission@example.gov",
        "Commissioner One",
        "test-password-123",
    )
    monkeypatch.setenv("POWER_EMAIL_BACKEND", "memory")
    monkeypatch.setenv("POWER_PUBLIC_BASE_URL", "https://app.homeenergywatch.com")
    app.EMAIL_OUTBOX.clear()

    existing_session = app.web_app.test_client()
    sign_in(existing_session)
    reset_client = app.web_app.test_client()
    known = reset_client.post("/staff/forgot-password", data={"email": "commission@example.gov"})
    token = token_from_latest_email()
    with app.get_db_connection() as conn:
        saved_token = conn.execute(
            "SELECT token_hash FROM staff_auth_tokens WHERE purpose = 'password_reset'"
        ).fetchone()["token_hash"]
    unknown = app.web_app.test_client().post(
        "/staff/forgot-password",
        data={"email": "nobody@example.gov"},
    )

    assert known.status_code == 200
    assert unknown.status_code == 200
    assert b"If commission access uses that address" in known.data
    assert b"If commission access uses that address" in unknown.data
    assert b"commission@example.gov" not in known.data
    assert b"nobody@example.gov" not in unknown.data
    assert saved_token != token
    assert token not in saved_token

    reset = reset_client.post(
        "/staff/reset-password",
        data={
            "token": token,
            "password": "new-commission-password-456",
            "password_confirm": "new-commission-password-456",
        },
    )
    replay = reset_client.post(
        "/staff/reset-password",
        data={
            "token": token,
            "password": "another-password-789",
            "password_confirm": "another-password-789",
        },
    )

    assert reset.status_code == 302
    assert reset.headers["Location"].endswith("/login")
    assert replay.status_code == 400
    assert b"This link is no longer available" in replay.data
    with pytest.raises(ValueError):
        app.authenticate_staff_user("commission@example.gov", "test-password-123")
    assert app.authenticate_staff_user(
        "commission@example.gov", "new-commission-password-456"
    )["email"] == "commission@example.gov"
    stale_session = existing_session.get("/")
    assert stale_session.status_code == 302
    assert "/login" in stale_session.headers["Location"]


def test_staff_mfa_encrypts_secret_hashes_recovery_codes_and_rejects_replay(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    staff_user = app.create_first_staff_user(
        "commission@example.gov",
        "Commissioner One",
        "test-password-123",
    )

    enrollment, result = enroll_staff_mfa(int(staff_user["id"]))
    recovery_codes = result["recovery_codes"]
    with app.get_db_connection() as conn:
        saved_user = conn.execute(
            """
            SELECT mfa_secret_token, mfa_pending_secret_token, mfa_enabled_at, mfa_last_counter
            FROM staff_users WHERE id = ?
            """,
            (int(staff_user["id"]),),
        ).fetchone()
        saved_codes = conn.execute(
            "SELECT code_hash, consumed_at FROM staff_mfa_recovery_codes WHERE staff_user_id = ?",
            (int(staff_user["id"]),),
        ).fetchall()

    assert enrollment["qr_data_uri"].startswith("data:image/png;base64,")
    assert result["staff_user"]["mfa_enabled"] is True
    assert saved_user["mfa_secret_token"].startswith("fernet:v1:")
    assert enrollment["secret"] not in saved_user["mfa_secret_token"]
    assert saved_user["mfa_pending_secret_token"] is None
    assert saved_user["mfa_enabled_at"]
    assert saved_user["mfa_last_counter"] is not None
    assert len(recovery_codes) == app.MFA_RECOVERY_CODE_COUNT
    assert len(saved_codes) == app.MFA_RECOVERY_CODE_COUNT
    assert all(code not in {row["code_hash"] for row in saved_codes} for code in recovery_codes)

    current_code = pyotp.TOTP(enrollment["secret"]).now()
    assert app.verify_staff_mfa_code(int(staff_user["id"]), current_code) is None
    assert app.verify_staff_mfa_code(int(staff_user["id"]), recovery_codes[0]) == "recovery_code"
    assert app.verify_staff_mfa_code(int(staff_user["id"]), recovery_codes[0]) is None
    assert app.count_staff_mfa_recovery_codes(int(staff_user["id"])) == app.MFA_RECOVERY_CODE_COUNT - 1


def test_staff_login_requires_mfa_challenge_before_session(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    staff_user = app.create_first_staff_user(
        "commission@example.gov",
        "Commissioner One",
        "test-password-123",
    )
    enrollment, _ = enroll_staff_mfa(int(staff_user["id"]))
    base_epoch = int(app.time.time())
    next_counter = (base_epoch // 30) + 1
    monkeypatch.setattr(app.time, "time", lambda: float(base_epoch + 30))
    next_code = pyotp.TOTP(enrollment["secret"]).generate_otp(next_counter)
    client = app.web_app.test_client()

    password_step = client.post(
        "/login",
        data={"email": "commission@example.gov", "password": "test-password-123"},
        follow_redirects=False,
    )
    with client.session_transaction() as browser_session:
        pending_id = browser_session.get("pending_staff_user_id")
        signed_in_id = browser_session.get("staff_user_id")
    blocked_workspace = client.get("/", follow_redirects=False)
    challenge_page = client.get("/staff/mfa/challenge")
    verified = client.post(
        "/staff/mfa/challenge",
        data={"code": next_code},
        follow_redirects=False,
    )
    with client.session_transaction() as browser_session:
        final_staff_id = browser_session.get("staff_user_id")
        final_pending_id = browser_session.get("pending_staff_user_id")
    with app.get_db_connection() as conn:
        event = conn.execute(
            "SELECT metadata_json FROM audit_events WHERE action = 'staff.login_succeeded' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert password_step.status_code == 302
    assert password_step.headers["Location"].endswith("/staff/mfa/challenge")
    assert pending_id == staff_user["id"]
    assert signed_in_id is None
    assert blocked_workspace.status_code == 302
    assert "/login" in blocked_workspace.headers["Location"]
    assert challenge_page.status_code == 200
    assert b"Verify your sign-in" in challenge_page.data
    assert verified.status_code == 302
    assert verified.headers["Location"].endswith("/")
    assert final_staff_id == staff_user["id"]
    assert final_pending_id is None
    assert json.loads(event["metadata_json"])["mfa"] == "authenticator"


def test_staff_mfa_recovery_code_completes_login_once(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    staff_user = app.create_first_staff_user(
        "commission@example.gov",
        "Commissioner One",
        "test-password-123",
    )
    _, result = enroll_staff_mfa(int(staff_user["id"]))
    recovery_code = result["recovery_codes"][0]
    client = app.web_app.test_client()

    client.post(
        "/login",
        data={"email": "commission@example.gov", "password": "test-password-123"},
    )
    first_use = client.post("/staff/mfa/challenge", data={"code": recovery_code})
    client.post("/logout")
    client.post(
        "/login",
        data={"email": "commission@example.gov", "password": "test-password-123"},
    )
    replay = client.post("/staff/mfa/challenge", data={"code": recovery_code})

    assert first_use.status_code == 302
    assert first_use.headers["Location"].endswith("/")
    assert replay.status_code == 400
    assert b"did not work" in replay.data


def test_required_staff_mfa_limits_unenrolled_sessions_to_security(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("POWER_STAFF_MFA_REQUIRED", "true")
    app.web_app.config["TESTING"] = True
    app.create_first_staff_user(
        "commission@example.gov",
        "Commissioner One",
        "test-password-123",
    )
    client = app.web_app.test_client()

    login_response = client.post(
        "/login",
        data={"email": "commission@example.gov", "password": "test-password-123"},
    )
    workspace = client.get("/", follow_redirects=False)
    api = client.get("/api/not-a-real-route")
    security = client.get("/staff/security")

    assert login_response.status_code == 302
    assert workspace.status_code == 302
    assert workspace.headers["Location"].endswith("/staff/security")
    assert api.status_code == 403
    assert api.get_json()["error"] == "Set up authenticator protection to continue."
    assert security.status_code == 200
    assert b"Set up an authenticator before opening" in security.data


def test_staff_mfa_change_revokes_other_sessions(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    staff_user = app.create_first_staff_user(
        "commission@example.gov",
        "Commissioner One",
        "test-password-123",
    )
    existing_session = app.web_app.test_client()
    sign_in(existing_session)
    enrollment = app.begin_staff_mfa_enrollment(int(staff_user["id"]))
    result = app.confirm_staff_mfa_enrollment(
        int(staff_user["id"]),
        pyotp.TOTP(enrollment["secret"]).now(),
    )

    stale = existing_session.get("/", follow_redirects=False)

    assert result["staff_user"]["mfa_enabled"] is True
    assert stale.status_code == 302
    assert "/login" in stale.headers["Location"]


def test_audit_record_is_commissioner_only_and_hides_remote_address(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    commissioner = app.create_first_staff_user(
        "commission@example.gov",
        "Commissioner One",
        "test-password-123",
    )
    invite = app.invite_staff_user(
        "analyst@example.gov",
        "Analyst One",
        "Analyst",
        invited_by_id=int(commissioner["id"]),
    )
    app.accept_staff_invite(invite["token"], "analyst-password-123")
    app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    client = app.web_app.test_client()
    client.post(
        "/customer/login",
        data={"email": "owner@example.com", "password": "wrong-password"},
        environ_base={"REMOTE_ADDR": "203.0.113.42"},
    )
    with app.get_db_connection() as conn:
        event = conn.execute(
            "SELECT remote_hash FROM audit_events WHERE action = 'customer.login_failed'"
        ).fetchone()

    commissioner_client = app.web_app.test_client()
    commissioner_client.post(
        "/login",
        data={"email": "commission@example.gov", "password": "test-password-123"},
    )
    commissioner_page = commissioner_client.get("/audit")
    analyst_client = app.web_app.test_client()
    analyst_client.post(
        "/login",
        data={"email": "analyst@example.gov", "password": "analyst-password-123"},
    )
    analyst_page = analyst_client.get("/audit")

    assert event["remote_hash"]
    assert event["remote_hash"] != "203.0.113.42"
    assert commissioner_page.status_code == 200
    assert b"customer.login_failed" in commissioner_page.data
    assert analyst_page.status_code == 302
    assert analyst_page.headers["Location"].endswith("/")


def test_account_changes_create_account_scoped_audit_events(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    stub_utility_lookup(monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()
    sign_in(client)

    account_response = client.post(
        "/account",
        data={
            "account_number": "audit-account",
            "address": "123 Main Street Charlotte NC",
            "zip_code": "28205",
            "baseline_date": "2026-07-01",
        },
    )
    inventory_response = client.post(
        "/load-items",
        data={
            "account_number": "audit-account",
            "label": "Kitchen refrigerator",
            "quantity": "1",
            "watts_each": "150",
            "include_when_off": "on",
        },
    )
    app.save_account_profile("other-account", display_name="Other account")
    app.record_audit_event(
        "account.profile_updated",
        actor_type="system",
        account_number="other-account",
    )
    audit_page = app.list_audit_events(account_number="audit-account")
    actions = {event["action"] for event in audit_page["events"]}

    assert account_response.status_code == 302
    assert inventory_response.status_code == 302
    assert "account.profile_updated" in actions
    assert "inventory.item_saved" in actions
    assert all(event["account_number"] == "audit-account" for event in audit_page["events"])


def test_audit_chain_detects_record_changes(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.save_account_profile("audit-account", display_name="Audit account")
    first_id = app.record_audit_event(
        "account.profile_updated",
        account_number="audit-account",
        metadata={"field": "address"},
    )
    second_id = app.record_audit_event(
        "history.imported",
        account_number="audit-account",
        metadata={"rows": 48},
    )

    status = app.verify_audit_chain()
    with app.get_db_connection() as conn:
        rows = conn.execute(
            "SELECT id, previous_hash, event_hash FROM audit_events ORDER BY id"
        ).fetchall()

    assert status["valid"] is True
    assert status["checked_events"] == 2
    assert rows[0]["id"] == first_id
    assert rows[0]["previous_hash"] is None
    assert rows[1]["id"] == second_id
    assert rows[1]["previous_hash"] == rows[0]["event_hash"]

    with app.get_db_connection() as conn:
        conn.execute(
            "UPDATE audit_events SET metadata_json = ? WHERE id = ?",
            ('{"rows":999}', first_id),
        )
        conn.commit()

    tampered = app.verify_audit_chain()
    assert tampered["valid"] is False
    assert tampered["failure_event_id"] == first_id


def test_audit_export_is_filtered_safe_and_commissioner_only(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    commissioner = app.create_first_staff_user(
        "commission@example.gov",
        "Commissioner One",
        "test-password-123",
    )
    invite = app.invite_staff_user(
        "analyst@example.gov",
        "Analyst One",
        "Analyst",
        invited_by_id=int(commissioner["id"]),
    )
    app.accept_staff_invite(invite["token"], "analyst-password-123")
    app.save_account_profile("account-a", display_name="Account A")
    app.save_account_profile("account-b", display_name="Account B")
    app.record_audit_event(
        "history.imported",
        account_number="account-a",
        target_type="file",
        target_id="=unsafe.csv",
    )
    app.record_audit_event("history.imported", account_number="account-b", target_type="file")

    commissioner_client = app.web_app.test_client()
    commissioner_client.post(
        "/login",
        data={"email": "commission@example.gov", "password": "test-password-123"},
    )
    response = commissioner_client.get(
        "/audit/export.csv",
        query_string={"account_number": "account-a", "action": "history.imported"},
    )
    rows = list(csv.DictReader(StringIO(response.get_data(as_text=True))))

    assert response.status_code == 200
    assert "no-store" in response.headers["Cache-Control"]
    assert "home-energy-watch-audit-" in response.headers["Content-Disposition"]
    assert len(rows) == 1
    assert rows[0]["account_number"] == "account-a"
    assert rows[0]["target_id"] == "'=unsafe.csv"
    assert rows[0]["event_hash"]
    assert "account-b" not in response.get_data(as_text=True)

    analyst_client = app.web_app.test_client()
    analyst_client.post(
        "/login",
        data={"email": "analyst@example.gov", "password": "analyst-password-123"},
    )
    denied = analyst_client.get("/audit/export.csv")
    assert denied.status_code == 302
    assert denied.headers["Location"].endswith("/")


def test_audit_export_stops_when_integrity_fails(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    app.create_first_staff_user(
        "commission@example.gov",
        "Commissioner One",
        "test-password-123",
    )
    app.record_audit_event("system.checked", metadata={"result": "ok"})
    client = app.web_app.test_client()
    client.post(
        "/login",
        data={"email": "commission@example.gov", "password": "test-password-123"},
    )
    with app.get_db_connection() as conn:
        conn.execute(
            "UPDATE audit_events SET action = ? WHERE action = ?",
            ("system.changed", "system.checked"),
        )
        conn.commit()

    page = client.get("/audit")
    export = client.get("/audit/export.csv")

    assert page.status_code == 200
    assert b"Integrity check failed" in page.data
    assert export.status_code == 409
    assert b"did not pass its integrity check" in export.data


def test_utility_access_secrets_use_authenticated_encryption(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)

    first = app.seal_secret_value("customer-approved-secret")
    second = app.seal_secret_value("customer-approved-secret")

    assert first.startswith("fernet:v1:")
    assert second.startswith("fernet:v1:")
    assert first != second
    assert app.unseal_secret_value(first) == "customer-approved-secret"

    monkeypatch.setenv(
        "POWER_DATA_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(b"different-test-encryption-key-01").decode("ascii"),
    )
    with pytest.raises(ValueError, match="could not be read"):
        app.unseal_secret_value(first)


def test_legacy_utility_access_secret_remains_readable(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    plaintext = b"legacy-customer-secret"
    key_stream = app.build_legacy_secret_key_stream(len(plaintext))
    legacy_token = base64.urlsafe_b64encode(
        bytes(byte ^ key_stream[index] for index, byte in enumerate(plaintext))
    ).decode("ascii")

    assert app.unseal_secret_value(legacy_token) == plaintext.decode("utf-8")


def test_csrf_rejects_forged_form_and_accepts_rendered_token(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    monkeypatch.setitem(app.web_app.config, "CSRF_ENFORCE_TESTS", True)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()

    page = client.get("/first-run")
    with client.session_transaction() as browser_session:
        csrf_token = browser_session["_csrf_token"]

    forged = client.post(
        "/first-run",
        data={
            "email": "commission@example.gov",
            "full_name": "Commissioner One",
            "password": "test-password-123",
        },
    )
    accepted = client.post(
        "/first-run",
        data={
            "_csrf_token": csrf_token,
            "email": "commission@example.gov",
            "full_name": "Commissioner One",
            "password": "test-password-123",
        },
    )

    assert page.status_code == 200
    assert forged.status_code == 400
    assert b"form has expired" in forged.data
    assert accepted.status_code == 302
    assert app.count_staff_users() == 1


def test_csrf_exempts_signed_stripe_webhook(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    monkeypatch.setitem(app.web_app.config, "CSRF_ENFORCE_TESTS", True)
    app.web_app.config["TESTING"] = True
    install_fake_stripe(monkeypatch)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_home_energy_watch")
    FakeStripeWebhook.event = {"type": "unhandled.test", "data": {"object": {}}}

    response = app.web_app.test_client().post(
        "/stripe/webhook",
        data=b'{"id":"evt_test"}',
        headers={"Stripe-Signature": "t=123,v1=test"},
    )

    assert response.status_code == 200


def test_login_rate_limit_is_persistent_and_does_not_store_identity(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    app.create_customer_user("limited@example.com", "Rate Limited", "customer-password-123")
    client = app.web_app.test_client()

    responses = [
        client.post(
            "/customer/login",
            data={"email": "limited@example.com", "password": "wrong-password"},
        )
        for _ in range(app.AUTH_RATE_LIMIT_MAX_ATTEMPTS)
    ]
    correct_while_blocked = client.post(
        "/customer/login",
        data={"email": "limited@example.com", "password": "customer-password-123"},
    )
    with app.get_db_connection() as conn:
        saved_limit = conn.execute(
            "SELECT identity_hash, attempt_count FROM auth_rate_limits WHERE scope = ?",
            ("customer_login",),
        ).fetchone()

    assert all(response.status_code == 302 for response in responses[:-1])
    assert responses[-1].status_code == 429
    assert correct_while_blocked.status_code == 429
    assert int(saved_limit["attempt_count"]) == app.AUTH_RATE_LIMIT_MAX_ATTEMPTS
    assert "limited@example.com" not in str(saved_limit["identity_hash"])


def test_successful_login_rotates_session_state(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    customer = app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    client = app.web_app.test_client()
    with client.session_transaction() as browser_session:
        browser_session["attacker_marker"] = "must-not-survive"

    response = customer_sign_in(client)
    with client.session_transaction() as browser_session:
        saved_session = dict(browser_session)

    assert response.status_code == 302
    assert saved_session["customer_user_id"] == int(customer["id"])
    assert saved_session["_permanent"] is True
    assert "attacker_marker" not in saved_session


def test_login_rejects_protocol_relative_next_redirect(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    client = app.web_app.test_client()

    response = client.post(
        "/customer/login",
        data={
            "email": "owner@example.com",
            "password": "customer-password-123",
            "next": "//malicious.example.test/collect",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/customer")
    assert "malicious.example.test" not in response.headers["Location"]


def test_security_headers_and_cookie_flags_are_set(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    response = app.web_app.test_client().get("/first-run")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert "HttpOnly" in response.headers["Set-Cookie"]
    assert "SameSite=Lax" in response.headers["Set-Cookie"]


def test_customer_report_downloads_are_bound_to_account_access(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    app.save_account_profile("account-a", display_name="Household A")
    app.save_account_profile("account-b", display_name="Household B")
    app.create_customer_user("a@example.com", "Customer A", "customer-password-123")
    app.create_customer_user("b@example.com", "Customer B", "customer-password-123")
    app.add_account_access_email("account-a", "a@example.com", access_level="Manager")
    app.add_account_access_email("account-b", "b@example.com", access_level="Manager")
    report_path = tmp_path / "output" / "account-a-report.csv"
    report_path.write_text("date,total_kwh\n2026-01-01,12.3\n", encoding="utf-8")
    unregistered_path = tmp_path / "output" / "old-unregistered-report.csv"
    unregistered_path.write_text("private\n", encoding="utf-8")
    app.register_report_artifacts("account-a", [report_path])

    customer_a = app.web_app.test_client()
    customer_b = app.web_app.test_client()
    assert customer_sign_in(customer_a, "a@example.com").status_code == 302
    assert customer_sign_in(customer_b, "b@example.com").status_code == 302

    allowed = customer_a.get(f"/reports/{report_path.name}")
    denied = customer_b.get(f"/reports/{report_path.name}")
    unregistered = customer_a.get(f"/reports/{unregistered_path.name}")

    assert allowed.status_code == 200
    assert b"2026-01-01,12.3" in allowed.data
    assert denied.status_code == 404
    assert unregistered.status_code == 404


def test_generated_report_names_do_not_collide_within_the_same_second(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)

    first = app.build_output_path(Path("combined-history.xml"))
    second = app.build_output_path(Path("combined-history.xml"))
    first_comparison = app.build_compare_output_path(Path("earlier.xml"), Path("later.xml"))
    second_comparison = app.build_compare_output_path(Path("earlier.xml"), Path("later.xml"))

    assert first != second
    assert first_comparison != second_comparison


def test_supported_feeds_endpoint_returns_registry(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()

    response = client.get("/api/supported-feeds")
    payload = response.get_json()

    assert response.status_code == 200
    assert {item["adapter_id"] for item in payload["supported_feeds"]} == {
        "green_button_espi",
        "duke_style_interval_xml",
        "utility_interval_csv",
    }


def test_utility_access_guides_cover_manual_connect_and_ncuc_paths():
    guides = app.list_utility_access_guides()

    assert {guide["id"] for guide in guides} == {
        "duke_download",
        "green_button_connect",
        "ncuc_data_access",
    }
    assert any("Duke My Account" in guide["action_label"] for guide in guides)
    assert any("Green Button" in guide["name"] for guide in guides)
    assert any("NCUC" in guide["action_label"] for guide in guides)


def test_load_day_weather_uses_geocoded_address_and_cache(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.save_account_profile("acct-1", display_name="Test Home")
    app.save_household_profile(
        "acct-1",
        {
            "address": "123 Main St Charlotte NC",
            "occupant_count": "3",
            "year_built": "1989",
            "square_footage": "2200",
            "heating_system": "Heat pump",
            "cooling_system": "Central air",
            "water_heater": "Electric tank",
            "notes": "Pool pump and garage fridge",
        },
    )

    calls = {"count": 0}

    def fake_fetch_json(url):
        calls["count"] += 1
        if "geocoding-api" in url:
            return {
                "results": [
                    {
                        "latitude": 35.2271,
                        "longitude": -80.8431,
                        "name": "Charlotte",
                        "admin1": "North Carolina",
                        "country": "United States",
                    }
                ]
            }
        return {
            "hourly": {
                "time": ["2024-01-01T00:00", "2024-01-01T01:00"],
                "temperature_2m": [41.2, 40.1],
                "apparent_temperature": [39.7, 38.4],
                "precipitation": [0.0, 0.1],
                "weather_code": [3, 61],
                "cloud_cover": [84, 90],
                "wind_speed_10m": [7.2, 8.4],
            }
        }

    monkeypatch.setattr(app, "fetch_json", fake_fetch_json)

    weather = app.load_day_weather("acct-1", "2024-01-01", "America/New_York")
    cached_weather = app.load_day_weather("acct-1", "2024-01-01", "America/New_York")
    profile = app.load_household_profile("acct-1")

    assert weather["available"] is True
    assert weather["location_name"] == "Charlotte, North Carolina, United States"
    assert weather["summary"]["high_temp_f"] == 41.2
    assert len(weather["hourly"]) == 2
    assert cached_weather["summary"]["conditions"] == "Cloudy"
    assert calls["count"] == 2
    assert round(float(profile["latitude"]), 4) == 35.2271
    assert profile["weather_location"] == "Charlotte, North Carolina, United States"


def test_build_weather_context_classifies_heat_storm_and_mild_days():
    hot = app.build_weather_context(
        {
            "available": True,
            "location_name": "Charlotte, North Carolina, United States",
            "summary": {
                "high_temp_f": 95.0,
                "low_temp_f": 76.0,
                "high_apparent_f": 101.2,
                "precipitation_in": 0.0,
                "max_wind_mph": 9.0,
                "conditions": "Clear",
            },
        }
    )
    storm = app.build_weather_context(
        {
            "available": True,
            "location_name": "Charlotte, North Carolina, United States",
            "summary": {
                "high_temp_f": 67.0,
                "low_temp_f": 54.0,
                "high_apparent_f": 67.0,
                "precipitation_in": 0.8,
                "max_wind_mph": 31.0,
                "conditions": "Thunderstorm",
            },
        }
    )
    mild = app.build_weather_context(
        {
            "available": True,
            "location_name": "Charlotte, North Carolina, United States",
            "summary": {
                "high_temp_f": 72.0,
                "low_temp_f": 55.0,
                "high_apparent_f": 72.0,
                "precipitation_in": 0.0,
                "max_wind_mph": 7.0,
                "conditions": "Mostly clear",
            },
        }
    )

    assert hot["signals"] == ["unusual_heat"]
    assert hot["effect"] == "plausible_explanation"
    assert "hot weather" in hot["summary"].lower()
    assert storm["signals"] == ["storm_conditions"]
    assert storm["effect"] == "plausible_explanation"
    assert "storm" in storm["summary"].lower()
    assert mild["signals"] == []
    assert mild["effect"] == "makes_spike_stand_out"
    assert "stand out" in mild["summary"].lower()


def test_ensure_schema_ready_uses_postgres_advisory_lock(monkeypatch):
    executed = []
    migrated = {"count": 0}

    class FakeConn:
        kind = "postgres"
        target_label = "postgresql://home-energy-watch@db.example/homeenergywatch"

        def execute(self, query, params=None):
            executed.append((query, params))
            return None

        def commit(self):
            migrated["committed"] = migrated.get("committed", 0) + 1

        def rollback(self):
            migrated["rolled_back"] = migrated.get("rolled_back", 0) + 1

    def fake_migrate(conn):
        assert conn.kind == "postgres"
        migrated["count"] += 1

    app.SCHEMA_READY_TARGETS.clear()
    monkeypatch.setattr(app, "migrate_database", fake_migrate)

    app.ensure_schema_ready(FakeConn())

    assert executed == [("SELECT pg_advisory_xact_lock(?)", (app.POSTGRES_SCHEMA_LOCK_KEY,))]
    assert migrated["count"] == 1
    assert migrated["committed"] == 1
    assert "postgresql://home-energy-watch@db.example/homeenergywatch" in app.SCHEMA_READY_TARGETS


def test_day_detail_api_returns_series_and_inventory(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True

    history = tmp_path / "input" / "history.xml"
    history.write_bytes(FIXTURE.read_bytes())
    app.import_interval_file_to_db(history, account_number="acct-1", display_name="Test Home")
    app.save_account_profile("acct-1", display_name="Test Home", baseline_date="2024-01-02")
    app.add_load_item("acct-1", label="Water heater", quantity=1, watts_each=4500, include_when_off=False)
    app.add_load_item("acct-1", label="Router", quantity=1, watts_each=15, include_when_off=True)

    client = app.web_app.test_client()
    sign_in(client)
    response = client.get(
        "/api/day-detail",
        query_string={
            "account_number": "acct-1",
            "date": "2024-01-01",
            "tz": "America/New_York",
            "night_start": "02:00",
            "night_end": "04:00",
            "min_night_kw": "1.0",
            "night_multiplier": "2.0",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["date"] == "2024-01-01"
    assert payload["series"]["current"]
    assert payload["previous_day"] is None
    assert payload["baseline_day"]["date"] == "2024-01-02"
    assert payload["load_summary"]["all_on_kw"] == 4.515


def test_history_page_offers_two_file_comparison_upload(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True

    client = app.web_app.test_client()
    sign_in(client)

    response = client.get("/history")

    assert response.status_code == 200
    assert b"Add history" in response.data
    assert b'name="xml_file"' in response.data
    assert b"Compare two exports" in response.data
    assert b'action="/compare"' in response.data
    assert b'name="left_file"' in response.data
    assert b'name="right_file"' in response.data
    assert b"Download a packet with matched months" in response.data
    assert b"Customer data permission must be active" in response.data


def test_staff_cannot_upload_or_compare_history_without_customer_permission(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    app.save_account_profile("acct-1", display_name="Main house")
    client = app.web_app.test_client()
    sign_in(client)

    upload = client.post(
        "/analyze",
        data={
            "account_number": "acct-1",
            "xml_file": (BytesIO(FIXTURE.read_bytes()), "history.xml"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    comparison = client.post(
        "/compare",
        data={
            "account_number": "acct-1",
            "left_file": (BytesIO(comparison_csv([])), "earlier.csv"),
            "right_file": (BytesIO(comparison_csv([])), "later.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert upload.status_code == 200
    assert b"Customer data permission is required before new history can be added" in upload.data
    assert comparison.status_code == 200
    assert b"Customer data permission is required before exports can be compared" in comparison.data
    assert app.count_imported_files("acct-1") == 0
    assert list((tmp_path / "input").iterdir()) == []


def test_web_comparison_upload_creates_downloadable_packet(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    authorize_account("acct-1")

    client = app.web_app.test_client()
    sign_in(client)

    left_csv = comparison_csv(
        [
            ("2024-01-01T02:00:00-05:00", "2024-01-01T03:00:00-05:00", 0.5),
            ("2024-01-01T03:00:00-05:00", "2024-01-01T04:00:00-05:00", 0.5),
            ("2024-01-02T02:00:00-05:00", "2024-01-02T03:00:00-05:00", 0.6),
            ("2024-01-02T03:00:00-05:00", "2024-01-02T04:00:00-05:00", 0.6),
        ]
    )
    right_csv = comparison_csv(
        [
            ("2024-01-01T02:00:00-05:00", "2024-01-01T03:00:00-05:00", 1.2),
            ("2024-01-01T03:00:00-05:00", "2024-01-01T04:00:00-05:00", 1.2),
            ("2024-01-02T02:00:00-05:00", "2024-01-02T03:00:00-05:00", 1.4),
            ("2024-01-02T03:00:00-05:00", "2024-01-02T04:00:00-05:00", 1.4),
        ]
    )

    response = client.post(
        "/compare",
        data={
            "account_number": "acct-1",
            "display_name": "Main house",
            "energy_company": "Duke Energy Carolinas, LLC",
            "baseline_date": "",
            "left_file": (BytesIO(left_csv), "earlier.csv"),
            "right_file": (BytesIO(right_csv), "later.csv"),
            "tz": "America/New_York",
            "night_start": "02:00",
            "night_end": "04:00",
            "min_night_kw": "1.0",
            "night_multiplier": "2.0",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert b"Comparison packet" in response.data
    assert b"Matched months" in response.data
    assert b"Total kWh change" in response.data
    assert b"Download Markdown" in response.data
    assert b"Download CSV" in response.data

    packets = list((tmp_path / "output").glob("*.md"))
    csv_artifacts = list((tmp_path / "output").glob("*.csv"))
    assert len(packets) == 1
    assert len(csv_artifacts) == 1
    packet_text = packets[0].read_text()
    csv_text = csv_artifacts[0].read_text()
    assert "# Duke interval comparison" in packet_text
    assert "- Matched months: 1" in packet_text
    assert "- Total kWh: 2.2 -> 5.2 (+3.0 / +136.4%)" in packet_text
    assert "- Overnight baseline: 0.55 kW -> 1.30 kW (+0.75 kW / +136.4%)" in packet_text
    assert "- Flagged nights: 0 -> 2 (+2)" in packet_text
    assert "Biggest follow-up points:" in packet_text
    assert "comparison_label" in csv_text
    assert "Jan 2024 vs Jan 2024" in csv_text

    download = client.get(f"/reports/{packets[0].name}")
    assert download.status_code == 200
    assert b"# Duke interval comparison" in download.data
    assert "attachment" in download.headers["Content-Disposition"]

    csv_download = client.get(f"/reports/{csv_artifacts[0].name}")
    assert csv_download.status_code == 200
    assert b"comparison_label" in csv_download.data
    assert "attachment" in csv_download.headers["Content-Disposition"]


def test_database_settings_redact_postgres_password(monkeypatch):
    monkeypatch.setenv("POWER_DATABASE_URL", "postgresql://meter_user:super-secret@db.example.com:5432/power_watch")

    settings = app.get_database_settings()
    status = app.build_database_status()

    assert settings["kind"] == "postgres"
    assert settings["target_label"] == "postgresql://meter_user@db.example.com:5432/power_watch"
    assert status["database_backend"] == "postgres"
    assert "super-secret" not in status["database_target"]


def test_web_routes_render_and_analyze(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True

    client = app.web_app.test_client()

    home = client.get("/")
    assert home.status_code == 302
    assert home.headers["Location"].endswith("/first-run")

    sign_in(client)
    authorize_account("acct-1")
    home = client.get("/")
    assert home.status_code == 200
    assert b"Review" in home.data
    assert b"Add history to start the review." in home.data
    assert b"Commission access" not in home.data
    assert b"House load list" not in home.data

    assert b"Commission access" in client.get("/staff").data
    assert b"People with account access" in client.get("/people").data
    assert b"Utility data connection" in client.get("/utility").data
    assert b"House load list" in client.get("/inventory").data
    assert b"Files that work today" in client.get("/history").data
    assert b"Duke Energy history" in client.get("/history").data
    assert b"Interval spreadsheet" in client.get("/history").data

    with FIXTURE.open("rb") as handle:
        response = client.post(
            "/analyze",
            data={
                "account_number": "acct-1",
                "display_name": "Main house",
                "baseline_date": "2024-01-02",
                "xml_file": (handle, "sample_interval.xml"),
                "tz": "America/New_York",
                "night_start": "02:00",
                "night_end": "04:00",
                "min_night_kw": "1.0",
                "night_multiplier": "2.0",
            },
            content_type="multipart/form-data",
        )

    assert response.status_code == 200
    assert b"Selected day" in response.data
    assert b"Click a day to see the curve" in response.data
    assert b"All-on check" in response.data
    assert b"Load test" in response.data
    assert b"Download CSV" in response.data
    assert b"Download JSON" in response.data
    assert list((tmp_path / "output").glob("*.csv"))
    assert list((tmp_path / "output").glob("*.json"))


def test_index_shows_latest_analysis_for_default_account(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True

    history = tmp_path / "input" / "history.xml"
    history.write_bytes(FIXTURE.read_bytes())
    app.import_interval_file_to_db(history)

    client = app.web_app.test_client()
    sign_in(client)
    home = client.get("/")

    assert home.status_code == 200
    assert b"Commission Review" in home.data
    assert b"Start here." in home.data
    assert b"Selected day" in home.data


def test_commissioner_can_invite_staff(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True

    client = app.web_app.test_client()
    sign_in(client)
    response = client.post(
        "/staff/invite",
        data={
            "full_name": "Analyst Two",
            "email": "analyst@example.gov",
            "role": "Analyst",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Latest setup link" in response.data
    assert b"Analyst Two" in response.data


def test_commissioner_can_suspend_staff_and_revoke_their_session(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    commissioner = app.create_first_staff_user(
        "commission@example.gov",
        "Commissioner One",
        "test-password-123",
    )
    invite = app.invite_staff_user(
        "analyst@example.gov",
        "Analyst One",
        "Analyst",
        invited_by_id=int(commissioner["id"]),
    )
    analyst = app.accept_staff_invite(invite["token"], "analyst-password-123")
    analyst_client = app.web_app.test_client()
    analyst_client.post(
        "/login",
        data={"email": "analyst@example.gov", "password": "analyst-password-123"},
    )
    commissioner_client = app.web_app.test_client()
    commissioner_client.post(
        "/login",
        data={"email": "commission@example.gov", "password": "test-password-123"},
    )

    response = commissioner_client.post(
        f"/staff/{analyst['id']}/access",
        data={"role": "Analyst", "status": "inactive"},
        follow_redirects=True,
    )
    updated = app.get_staff_user_by_id(int(analyst["id"]))
    stale_session = analyst_client.get("/")
    with app.get_db_connection() as conn:
        event = conn.execute(
            "SELECT metadata_json FROM audit_events WHERE action = 'staff.access_updated' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert response.status_code == 200
    assert b"Access for Analyst One was updated" in response.data
    assert updated["is_active"] is False
    assert updated["auth_version"] > analyst["auth_version"]
    assert stale_session.status_code == 302
    assert "/login" in stale_session.headers["Location"]
    metadata = json.loads(event["metadata_json"])
    assert metadata["previous_status"] == "active"
    assert metadata["status"] == "inactive"


def test_staff_access_management_requires_commissioner_and_protects_self(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    commissioner = app.create_first_staff_user(
        "commission@example.gov",
        "Commissioner One",
        "test-password-123",
    )
    invite = app.invite_staff_user(
        "analyst@example.gov",
        "Analyst One",
        "Analyst",
        invited_by_id=int(commissioner["id"]),
    )
    analyst = app.accept_staff_invite(invite["token"], "analyst-password-123")
    commissioner_client = app.web_app.test_client()
    commissioner_client.post(
        "/login",
        data={"email": "commission@example.gov", "password": "test-password-123"},
    )

    self_change = commissioner_client.post(
        f"/staff/{commissioner['id']}/access",
        data={"role": "Analyst", "status": "inactive"},
        follow_redirects=True,
    )
    analyst_client = app.web_app.test_client()
    analyst_client.post(
        "/login",
        data={"email": "analyst@example.gov", "password": "analyst-password-123"},
    )
    forbidden = analyst_client.post(
        f"/staff/{commissioner['id']}/access",
        data={"role": "Analyst", "status": "inactive"},
        follow_redirects=False,
    )

    unchanged = app.get_staff_user_by_id(int(commissioner["id"]))
    assert self_change.status_code == 200
    assert b"Ask another commissioner to change your access" in self_change.data
    assert unchanged["role"] == "Commissioner"
    assert unchanged["is_active"] is True
    assert forbidden.status_code == 302
    assert forbidden.headers["Location"].endswith("/")


def test_commissioner_can_reset_other_staff_mfa_and_revoke_their_session(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    commissioner = app.create_first_staff_user(
        "commission@example.gov",
        "Commissioner One",
        "test-password-123",
    )
    invite = app.invite_staff_user(
        "analyst@example.gov",
        "Analyst One",
        "Analyst",
        invited_by_id=int(commissioner["id"]),
    )
    analyst = app.accept_staff_invite(invite["token"], "analyst-password-123")
    _, enrollment = enroll_staff_mfa(int(analyst["id"]))
    analyst_session = app.web_app.test_client()
    analyst_session.post(
        "/login",
        data={"email": "analyst@example.gov", "password": "analyst-password-123"},
    )
    analyst_session.post(
        "/staff/mfa/challenge",
        data={"code": enrollment["recovery_codes"][0]},
    )
    assert analyst_session.get("/").status_code == 200

    commissioner_session = app.web_app.test_client()
    commissioner_session.post(
        "/login",
        data={"email": "commission@example.gov", "password": "test-password-123"},
    )
    confirmation = commissioner_session.get(f"/staff/{analyst['id']}/mfa/reset")
    response = commissioner_session.post(
        f"/staff/{analyst['id']}/mfa/reset",
        follow_redirects=True,
    )
    updated = app.get_staff_user_by_id(int(analyst["id"]))
    stale_session = analyst_session.get("/", follow_redirects=False)
    with app.get_db_connection() as conn:
        event = conn.execute(
            """
            SELECT actor_id, target_id, metadata_json
            FROM audit_events
            WHERE action = 'staff.mfa_reset_by_commissioner'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()

    assert confirmation.status_code == 200
    assert b"Reset Analyst One's authenticator?" in confirmation.data
    assert response.status_code == 200
    assert b"Authenticator protection for Analyst One was reset" in response.data
    assert updated["mfa_enabled"] is False
    assert app.count_staff_mfa_recovery_codes(int(analyst["id"])) == 0
    assert updated["role"] == "Analyst"
    assert updated["is_active"] is True
    assert app.authenticate_staff_user("analyst@example.gov", "analyst-password-123")["id"] == analyst["id"]
    assert stale_session.status_code == 302
    assert "/login" in stale_session.headers["Location"]
    assert int(event["actor_id"]) == int(commissioner["id"])
    assert str(event["target_id"]) == str(analyst["id"])
    assert json.loads(event["metadata_json"])["target_role"] == "Analyst"

    monkeypatch.setenv("POWER_STAFF_MFA_REQUIRED", "true")
    fresh_session = app.web_app.test_client()
    fresh_session.post(
        "/login",
        data={"email": "analyst@example.gov", "password": "analyst-password-123"},
    )
    required_setup = fresh_session.get("/", follow_redirects=False)
    assert required_setup.status_code == 302
    assert required_setup.headers["Location"].endswith("/staff/security")


def test_commissioner_cannot_reset_their_own_mfa(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    commissioner = app.create_first_staff_user(
        "commission@example.gov",
        "Commissioner One",
        "test-password-123",
    )
    _, enrollment = enroll_staff_mfa(int(commissioner["id"]))
    client = app.web_app.test_client()
    client.post(
        "/login",
        data={"email": "commission@example.gov", "password": "test-password-123"},
    )
    client.post(
        "/staff/mfa/challenge",
        data={"code": enrollment["recovery_codes"][0]},
    )

    confirmation = client.get(
        f"/staff/{commissioner['id']}/mfa/reset",
        follow_redirects=True,
    )
    response = client.post(
        f"/staff/{commissioner['id']}/mfa/reset",
        follow_redirects=True,
    )

    assert confirmation.status_code == 200
    assert response.status_code == 200
    assert b"Another commissioner must reset your authenticator" in confirmation.data
    assert b"Another commissioner must reset your authenticator" in response.data
    assert app.get_staff_user_by_id(int(commissioner["id"]))["mfa_enabled"] is True
    assert app.count_staff_mfa_recovery_codes(int(commissioner["id"])) == app.MFA_RECOVERY_CODE_COUNT - 1


def test_analyst_cannot_reset_another_staff_members_mfa(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    commissioner = app.create_first_staff_user(
        "commission@example.gov",
        "Commissioner One",
        "test-password-123",
    )
    enroll_staff_mfa(int(commissioner["id"]))
    invite = app.invite_staff_user(
        "analyst@example.gov",
        "Analyst One",
        "Analyst",
        invited_by_id=int(commissioner["id"]),
    )
    analyst = app.accept_staff_invite(invite["token"], "analyst-password-123")
    client = app.web_app.test_client()
    client.post(
        "/login",
        data={"email": "analyst@example.gov", "password": "analyst-password-123"},
    )

    confirmation = client.get(
        f"/staff/{commissioner['id']}/mfa/reset",
        follow_redirects=False,
    )
    response = client.post(
        f"/staff/{commissioner['id']}/mfa/reset",
        follow_redirects=False,
    )

    assert confirmation.status_code == 302
    assert confirmation.headers["Location"].endswith("/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")
    assert app.get_staff_user_by_id(int(commissioner["id"]))["mfa_enabled"] is True
    assert app.get_staff_user_by_id(int(analyst["id"]))["role"] == "Analyst"
