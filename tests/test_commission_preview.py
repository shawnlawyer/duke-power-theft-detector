"""Commissioner preview of the commission (read-only, jurisdiction-scoped) view."""

import app
from tests.test_access_control import commission_sign_in, nc_account, sc_account
from tests.test_app import configure_tmp_paths, sign_in


def nc_commission_id():
    return next(item["id"] for item in app.list_commission_organizations() if item["jurisdiction_code"] == "NC")


def test_commissioner_can_preview_exactly_what_a_commission_sees(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    nc_account("acct-raleigh")
    sc_account("acct-columbia")
    client = app.web_app.test_client()
    sign_in(client)

    staff_page = client.get("/staff")
    assert b"See what a commission sees" in staff_page.data

    started = client.post("/staff/commission-preview", data={"commission_id": nc_commission_id()}, follow_redirects=False)
    assert started.status_code == 302

    review = client.get("/", query_string={"account_number": "acct-columbia"})
    assert review.status_code == 200
    assert b"commission-preview-banner" in review.data
    assert b"North Carolina Utilities Commission" in review.data
    assert b"acct-raleigh" in review.data
    assert b"acct-columbia" not in review.data
    assert b"Audit</span>" not in review.data
    assert b"Staff</span>" not in review.data
    assert b"Upload the export" not in review.data

    # Read-only and no Commissioner tools while previewing.
    blocked = client.post("/account-access", data={"account_number": "acct-raleigh", "email": "x@example.com"})
    assert blocked.status_code == 403
    assert client.get("/audit", follow_redirects=False).status_code == 302
    assert client.get("/api/files").status_code == 403

    stopped = client.post("/staff/commission-preview/stop", follow_redirects=False)
    assert stopped.status_code == 302
    after = client.get("/account", query_string={"account_number": "acct-columbia"})
    assert b"commission-preview-banner" not in after.data
    assert b"1100 Gervais" in after.data
    assert client.get("/audit").status_code == 200

    actions = [event["action"] for event in app.list_audit_events(page=1)["events"]]
    assert "staff.commission_preview_started" in actions
    assert "staff.commission_preview_ended" in actions


def test_preview_is_limited_to_the_browser_that_started_it(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    sc_account("acct-columbia")
    previewing = app.web_app.test_client()
    sign_in(previewing)
    previewing.post("/staff/commission-preview", data={"commission_id": nc_commission_id()})
    other = app.web_app.test_client()
    sign_in(other)

    page = other.get("/account", query_string={"account_number": "acct-columbia"})

    assert b"commission-preview-banner" not in page.data
    assert b"1100 Gervais" in page.data


def test_only_commissioners_can_start_a_preview(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()
    sign_in(client)
    with app.get_db_connection() as conn:
        conn.execute("UPDATE staff_users SET role = 'Analyst' WHERE email = ?", ("commission@example.gov",))
        conn.commit()

    client.post("/staff/commission-preview", data={"commission_id": nc_commission_id()})
    with client.session_transaction() as flask_session:
        assert app.COMMISSION_PREVIEW_SESSION_KEY not in flask_session

    reviewer = app.web_app.test_client()
    commission_sign_in(reviewer)
    reviewer.post("/staff/commission-preview", data={"commission_id": nc_commission_id()})
    with reviewer.session_transaction() as flask_session:
        assert app.COMMISSION_PREVIEW_SESSION_KEY not in flask_session


def test_preview_rejects_an_unknown_commission(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()
    sign_in(client)

    client.post("/staff/commission-preview", data={"commission_id": "99999"})

    with client.session_transaction() as flask_session:
        assert app.COMMISSION_PREVIEW_SESSION_KEY not in flask_session


def test_commission_reviewers_can_find_their_security_page(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()
    commission_sign_in(client)

    page = client.get("/")

    assert b"Security</span>" in page.data
    assert b"Staff</span>" not in page.data
