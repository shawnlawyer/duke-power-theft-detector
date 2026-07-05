import json
from io import BytesIO
from datetime import date as ddate
from pathlib import Path

import app
import pandas as pd


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
    app.SCHEMA_READY_TARGETS.clear()
    app.ensure_data_dirs()


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


def make_summary(rows):
    frame = pd.DataFrame(rows)
    frame["date"] = frame["date"].map(ddate.fromisoformat)
    return frame.set_index("date")


def comparison_csv(values):
    rows = ["interval_start,interval_end,usage_kwh"]
    for start, end, usage_kwh in values:
        rows.append(f"{start},{end},{usage_kwh}")
    return ("\n".join(rows) + "\n").encode()


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


def test_utility_connection_stores_customer_granted_access_without_exposing_secret(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)

    connection = app.save_utility_connection(
        "acct-1",
        {
            "provider_name": "Duke Energy",
            "connection_label": "Main Duke login",
            "access_method": "customer_api_key",
            "access_identifier": "customer@example.com",
            "access_secret": "sk_live_customer_meter_access_1234",
        },
    )

    assert connection["provider_name"] == "Duke Energy"
    assert connection["access_method"] == "customer_api_key"
    assert connection["access_identifier"] == "customer@example.com"
    assert connection["secret_last4"] == "1234"
    assert "sk_live" not in json.dumps(connection)
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
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()

    response = client.post(
        "/signup",
        data={
            "full_name": "Home Owner",
            "email": "owner@example.com",
            "password": "customer-password-123",
            "account_number": "duke-123",
            "energy_company": "Duke Energy Progress, LLC",
            "address": "123 Main St Charlotte NC",
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

    dashboard = client.get("/customer")

    assert dashboard.status_code == 200
    assert b"Your energy history" in dashboard.data
    assert b"Billing" in dashboard.data
    assert b"History" in dashboard.data
    assert b"Commission access" not in dashboard.data

    account_page = client.get("/customer/account")
    assert account_page.status_code == 200
    assert b"Duke Energy Progress, LLC" in account_page.data


def test_account_forms_use_energy_company_select_instead_of_home_name(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()

    sign_in(client)
    home = client.get("/account")
    signup = client.get("/signup")

    assert home.status_code == 200
    assert signup.status_code == 200
    assert b"Energy company" in home.data
    assert b"Energy company" in signup.data
    assert b"Duke Energy Carolinas, LLC" in home.data
    assert b"ENERGYUNITED EMC" in home.data
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


def test_billing_plans_use_configured_stripe_price_ids(monkeypatch):
    monkeypatch.setenv("STRIPE_PRICE_HOME", "price_home_123")
    monkeypatch.setenv("STRIPE_PRICE_REVIEW", "price_review_456")

    plans = app.list_billing_plans()
    home = next(plan for plan in plans if plan["id"] == "home")
    review = next(plan for plan in plans if plan["id"] == "review")

    assert home["price_id"] == "price_home_123"
    assert home["monthly_price_label"] == "$19/mo"
    assert home["account_limit"] == 1
    assert review["price_id"] == "price_review_456"
    assert review["account_limit"] == 20


def test_customer_signup_saves_selected_billing_plan(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
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
            "plan_id": "review",
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
    assert b"Start billing" in billing_page.data


def test_customer_checkout_redirects_to_stripe_subscription_session(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")
    monkeypatch.setenv("STRIPE_PRICE_HOME", "price_home_123")
    monkeypatch.setenv("POWER_PUBLIC_BASE_URL", "https://app.homeenergywatch.com")

    calls = {}

    class FakeSession:
        @staticmethod
        def create(**kwargs):
            calls.update(kwargs)
            return {"id": "cs_test_123", "url": "https://checkout.stripe.test/session"}

    class FakeCheckout:
        Session = FakeSession

    class FakeStripe:
        checkout = FakeCheckout
        api_key = None

    monkeypatch.setattr(app, "stripe", FakeStripe)
    customer = app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    app.record_customer_plan_selection(int(customer["id"]), "home")

    client = app.web_app.test_client()
    customer_sign_in(client)
    response = client.post("/billing/checkout", data={"plan_id": "home"}, follow_redirects=False)
    billing = app.load_customer_billing(int(customer["id"]))

    assert response.status_code == 302
    assert response.headers["Location"] == "https://checkout.stripe.test/session"
    assert calls["mode"] == "subscription"
    assert calls["line_items"] == [{"price": "price_home_123", "quantity": 1}]
    assert calls["customer_email"] == "owner@example.com"
    assert calls["metadata"]["customer_user_id"] == str(customer["id"])
    assert calls["success_url"] == "https://app.homeenergywatch.com/billing/success?session_id={CHECKOUT_SESSION_ID}"
    assert billing["checkout_session_id"] == "cs_test_123"
    assert billing["status"] == "checkout_started"


def test_stripe_webhook_updates_customer_subscription(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    customer = app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    app.record_customer_plan_selection(int(customer["id"]), "home")

    app.handle_billing_event(
        {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_123",
                    "customer": "cus_123",
                    "subscription": "sub_123",
                    "metadata": {"customer_user_id": str(customer["id"]), "plan_id": "home"},
                }
            },
        }
    )

    billing = app.load_customer_billing(int(customer["id"]))

    assert billing["stripe_customer_id"] == "cus_123"
    assert billing["stripe_subscription_id"] == "sub_123"
    assert billing["status"] == "active"


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
    assert b"Disallow: /" in app_robots.data


def test_marketing_pricing_page_uses_public_copy(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()

    response = client.get("/pricing", base_url="https://homeenergywatch.com")

    assert response.status_code == 200
    assert b"Plans that match the size of the review." in response.data
    assert b"Home Watch" in response.data
    assert b"Review Desk" in response.data


def test_health_endpoint_only_returns_public_status(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()

    response = client.get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


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


def test_web_comparison_upload_creates_downloadable_packet(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True

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
