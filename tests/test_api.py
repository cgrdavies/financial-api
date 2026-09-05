import json

import httpx
import pytest
from fastapi.testclient import TestClient

from financial_api.config import Item, Settings
from financial_api.main import create_app

TOKEN = "test-api-token-not-real-" + "x" * 32
SETTINGS = Settings(
    TOKEN,
    "test-client",
    "test-secret",
    "sandbox",
    (
        Item("item-a", "test-access-a", "Example Bank"),
        Item("item-b", "test-access-b", "Second Bank"),
    ),
)
AUTH = {"Authorization": f"Bearer {TOKEN}"}
TX = {
    "transaction_id": "tx-1",
    "account_id": "account-a",
    "date": "2026-01-02",
    "name": "Example merchant",
    "amount": -12.34,
    "iso_currency_code": "USD",
    "pending": False,
    "pending_transaction_id": "pending-1",
    "personal_finance_category": {"primary": "INCOME", "detailed": "INCOME_OTHER_INCOME"},
}
ACCOUNT = {"account_id": "account-a", "name": "Example checking", "balances": {"current": None}}


def client(handler):
    return TestClient(create_app(SETTINGS, httpx.MockTransport(handler)))


def no_upstream(request):
    raise AssertionError("This request must not reach Plaid")


@pytest.mark.parametrize("path", ["/items", "/accounts", "/transactions", "/openapi.json"])
@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic anything"}]
)
def test_auth_required(path, headers):
    with client(no_upstream) as api:
        response = api.get(path, headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.headers["cache-control"] == "no-store"


def test_health_and_items_do_not_leak_credentials():
    with client(no_upstream) as api:
        health = api.get("/health")
        assert health.json() == {"status": "ok"}
        response = api.get("/items", headers=AUTH)
        assert response.json() == {
            "items": [
                {"item_id": "item-a", "institution_name": "Example Bank"},
                {"item_id": "item-b", "institution_name": "Second Bank"},
            ]
        }
        assert "access" not in response.text
        assert "secret" not in response.text
        assert api.get("/docs").status_code == 404
        assert api.get("/openapi.json", headers=AUTH).status_code == 200


def test_accounts_uses_selected_connection():
    def handler(request):
        assert str(request.url) == "https://sandbox.plaid.com/accounts/get"
        assert request.headers["Plaid-Version"] == "2020-09-14"
        body = json.loads(request.content)
        assert body == {
            "client_id": "test-client",
            "secret": "test-secret",
            "access_token": "test-access-b",
        }
        return httpx.Response(200, json={"accounts": [ACCOUNT]})

    with client(handler) as api:
        response = api.get("/accounts?item_id=item-b", headers=AUTH)
        assert response.status_code == 200
        assert response.json()["item_id"] == "item-b"
        balance = response.json()["accounts"][0]["balance"]
        assert balance["current"] is None
        assert balance["currency"] is None


def test_transactions_page_and_normalization():
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/transactions/get"
        assert body["start_date"] == "2026-01-01"
        assert body["end_date"] == "2026-01-31"
        assert body["options"] == {"count": 1, "offset": 0}
        return httpx.Response(
            200,
            json={
                "transactions": [TX],
                "accounts": [ACCOUNT],
                "total_transactions": 2,
            },
        )

    with client(handler) as api:
        response = api.get(
            "/transactions?item_id=item-a&start_date=2026-01-01&end_date=2026-01-31&count=1",
            headers=AUTH,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["next_offset"] == 1
        assert data["total_transactions"] == 2
        tx = data["transactions"][0]
        assert tx["amount"] == -12.34
        assert tx["pending_transaction_id"] == "pending-1"
        assert tx["personal_finance_category"]["primary"] == "INCOME"
        assert tx["item_id"] == "item-a"


def test_empty_transaction_page():
    with client(
        lambda _: httpx.Response(
            200,
            json={
                "transactions": [],
                "accounts": [],
                "total_transactions": 0,
            },
        )
    ) as api:
        response = api.get(
            "/transactions?item_id=item-a&start_date=2026-01-01&end_date=2026-01-01", headers=AUTH
        )
        assert response.json()["next_offset"] is None


@pytest.mark.parametrize(
    "query",
    [
        "start_date=2026-02-30&end_date=2026-03-01",
        "start_date=2026-02-01&end_date=2026-01-01",
        "start_date=2026-01-01&end_date=2026-01-31&count=501",
        "start_date=2026-01-01&end_date=2026-01-31&count=0",
        "start_date=2026-01-01&end_date=2026-01-31&offset=-1",
    ],
)
def test_invalid_date_or_page(query):
    with client(no_upstream) as api:
        assert api.get(f"/transactions?item_id=item-a&{query}", headers=AUTH).status_code == 422


def test_unknown_item_never_reaches_plaid():
    with client(no_upstream) as api:
        assert api.get("/accounts?item_id=unknown", headers=AUTH).status_code == 404
        assert (
            api.post("/transactions/sync", json={"item_id": "unknown"}, headers=AUTH).status_code
            == 404
        )


def sync_page(cursor="next", more=False):
    return {
        "added": [TX],
        "modified": [],
        "removed": [{"transaction_id": "removed-1", "account_id": "account-a"}],
        "accounts": [ACCOUNT],
        "next_cursor": cursor,
        "has_more": more,
        "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
    }


def test_sync_pagination_is_caller_owned_and_replayable():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/transactions/sync"
        seen.append(body.get("cursor"))
        return httpx.Response(200, json=sync_page("page-2", body.get("cursor") != "page-2"))

    with client(handler) as api:
        for cursor in [None, "page-2", None]:
            body = {"item_id": "item-a"}
            if cursor is not None:
                body["cursor"] = cursor
            response = api.post("/transactions/sync", json=body, headers=AUTH)
            assert response.status_code == 200
            data = response.json()
            assert data["next_cursor"] == "page-2"
            assert data["has_more"] == (cursor is None)
            assert data["removed"] == [
                {"item_id": "item-a", "transaction_id": "removed-1", "account_id": "account-a"}
            ]
    assert seen == [None, "page-2", None]


@pytest.mark.parametrize(
    "body",
    [
        {"item_id": "item-a", "cursor": {"accidental-secret": "do-not-echo"}},
        {"item_id": "item-a", "access_token": "do-not-echo"},
        {"item_id": "item-a", "count": 501},
        {"item_id": "item-a", "count": "500"},
        {"item_id": "item-a", "cursor": "x" * 16385},
    ],
)
def test_sync_validation_scrubs_input(body):
    with client(no_upstream) as api:
        response = api.post("/transactions/sync", json=body, headers=AUTH)
        assert response.status_code == 422
        assert "do-not-echo" not in response.text
        assert "input" not in response.text


def test_sync_auth_and_body_limit():
    with client(no_upstream) as api:
        assert api.post("/transactions/sync", json={"item_id": "item-a"}).status_code == 401
        assert api.post("/transactions/sync", content=b"x" * 32769, headers=AUTH).status_code == 413


@pytest.mark.parametrize(
    "code,status,expected",
    [
        ("ITEM_LOGIN_REQUIRED", 400, 502),
        ("PRODUCT_NOT_READY", 400, 502),
        ("RATE_LIMIT_EXCEEDED", 429, 429),
        ("TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION", 400, 409),
        ("secret-do-not-echo", 400, 502),
    ],
)
def test_upstream_errors_are_safe(code, status, expected):
    with client(
        lambda _: httpx.Response(
            status,
            json={
                "error_code": code,
                "error_message": "do-not-echo test-access-a test-secret",
            },
        )
    ) as api:
        response = api.post("/transactions/sync", json={"item_id": "item-a"}, headers=AUTH)
        assert response.status_code == expected
        assert "do-not-echo" not in response.text
        assert "test-secret" not in response.text
        assert response.json()["error"]["restart_from_committed_cursor"] == (expected == 409)


@pytest.mark.parametrize("exception,status", [(httpx.ReadTimeout, 504), (httpx.ConnectError, 502)])
def test_network_failures(exception, status):
    def handler(request):
        raise exception("do-not-echo", request=request)

    with client(handler) as api:
        response = api.get("/accounts?item_id=item-a", headers=AUTH)
        assert response.status_code == status
        assert "do-not-echo" not in response.text


def test_redirect_not_followed_and_non_json_response():
    with client(
        lambda _: httpx.Response(302, headers={"Location": "https://example.invalid"})
    ) as api:
        response = api.get("/accounts?item_id=item-a", headers=AUTH)
        assert response.status_code == 502


def test_sync_empty_initial_result_keeps_status():
    result = {
        "added": [],
        "modified": [],
        "removed": [],
        "has_more": False,
        "next_cursor": "",
        "transactions_update_status": "NOT_READY",
    }
    with client(lambda _: httpx.Response(200, json=result)) as api:
        response = api.post("/transactions/sync", json={"item_id": "item-a"}, headers=AUTH)
        assert response.json()["transactions_update_status"] == "NOT_READY"
        assert response.json()["next_cursor"] == ""


def test_modified_pending_and_unofficial_currency_are_preserved():
    tx = {**TX, "pending": True, "iso_currency_code": None, "unofficial_currency_code": "BTC"}
    page = sync_page()
    page["added"] = []
    page["modified"] = [tx]
    with client(lambda _: httpx.Response(200, json=page)) as api:
        response = api.post("/transactions/sync", json={"item_id": "item-a"}, headers=AUTH)
        modified = response.json()["modified"][0]
        assert modified["pending"] is True
        assert modified["currency"] is None
        assert modified["unofficial_currency_code"] == "BTC"


def test_final_date_range_page():
    def handler(request):
        assert json.loads(request.content)["options"]["offset"] == 1
        return httpx.Response(
            200,
            json={
                "transactions": [TX],
                "accounts": [],
                "total_transactions": 2,
            },
        )

    with client(handler) as api:
        response = api.get(
            "/transactions?item_id=item-a&start_date=2026-01-01&end_date=2026-01-31&offset=1",
            headers=AUTH,
        )
        assert response.json()["next_offset"] is None


def test_nonadvancing_pagination_fails_without_returning_partial_data():
    with client(lambda _: httpx.Response(200, json=sync_page("same", True))) as api:
        response = api.post(
            "/transactions/sync", headers=AUTH, json={"item_id": "item-a", "cursor": "same"}
        )
        assert response.status_code == 502
        assert "added" not in response.json()
    with client(
        lambda _: httpx.Response(
            200,
            json={
                "transactions": [],
                "accounts": [],
                "total_transactions": 5,
            },
        )
    ) as api:
        response = api.get(
            "/transactions?item_id=item-a&start_date=2026-01-01&end_date=2026-01-31", headers=AUTH
        )
        assert response.status_code == 502


def test_malformed_json_body_does_not_echo_secret():
    with client(no_upstream) as api:
        response = api.post(
            "/transactions/sync",
            content=b'{"secret":do-not-echo}',
            headers={**AUTH, "Content-Type": "application/json"},
        )
        assert response.status_code == 422
        assert "do-not-echo" not in response.text
