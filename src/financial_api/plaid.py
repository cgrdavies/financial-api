"""Read-only Plaid adapter. No database, token exchange, webhooks, or stored cursors."""

import httpx

from .config import HOSTS, Item, Settings

# Deliberately do not relay Plaid's free-text errors or response bodies.
SAFE_CODES = {
    "ITEM_LOGIN_REQUIRED",
    "ITEM_LOCKED",
    "ITEM_NOT_SUPPORTED",
    "USER_PERMISSION_REVOKED",
    "NO_ACCOUNTS",
    "NO_AUTH_ACCOUNTS",
    "PRODUCT_NOT_READY",
    "PRODUCT_NOT_ENABLED",
    "ADDITIONAL_CONSENT_REQUIRED",
    "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION",
    "INVALID_CURSOR",
    "INVALID_ACCESS_TOKEN",
    "INVALID_API_KEYS",
    "INVALID_FIELD",
    "RATE_LIMIT_EXCEEDED",
    "INSTITUTION_DOWN",
    "INSTITUTION_NOT_RESPONDING",
    "INTERNAL_SERVER_ERROR",
    "PLANNED_MAINTENANCE",
}


class UpstreamError(Exception):
    def __init__(self, code: str, status: int = 502):
        self.code = code
        self.status = status
        super().__init__(code)


class Plaid:
    def __init__(self, settings: Settings, client: httpx.Client):
        self.settings = settings
        self.client = client

    def post(self, endpoint: str, item: Item, **params) -> dict:
        try:
            response = self.client.post(
                HOSTS[self.settings.plaid_environment] + endpoint,
                headers={"Plaid-Version": "2020-09-14"},
                json={
                    "client_id": self.settings.plaid_client_id,
                    "secret": self.settings.plaid_secret,
                    "access_token": item.access_token,
                    **params,
                },
            )
        except httpx.TimeoutException:
            raise UpstreamError("UPSTREAM_TIMEOUT", 504) from None
        except httpx.RequestError:
            raise UpstreamError("UPSTREAM_UNAVAILABLE", 502) from None
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            raise UpstreamError("INVALID_UPSTREAM_RESPONSE") from None
        if not response.is_success:
            code = data.get("error_code")
            code = code if isinstance(code, str) and code in SAFE_CODES else "PLAID_ERROR"
            status = 429 if response.status_code == 429 else 502
            if code == "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION":
                status = 409
            raise UpstreamError(code, status)
        return data

    def accounts(self, item: Item) -> dict:
        data = self.post("/accounts/get", item)
        return {
            "item_id": item.item_id,
            "accounts": [account(a, item) for a in data["accounts"]],
        }

    def transactions(self, item: Item, start: str, end: str, count: int, offset: int) -> dict:
        data = self.post(
            "/transactions/get",
            item,
            start_date=start,
            end_date=end,
            options={"count": count, "offset": offset},
        )
        transactions = [transaction(tx, item) for tx in data["transactions"]]
        next_offset = offset + len(transactions)
        total = data["total_transactions"]
        if not transactions and next_offset < total:
            raise UpstreamError("INVALID_UPSTREAM_RESPONSE")
        return {
            "item_id": item.item_id,
            "transactions": transactions,
            "accounts": [account(a, item) for a in data["accounts"]],
            "count": len(transactions),
            "total_transactions": total,
            "next_offset": next_offset if next_offset < total else None,
        }

    def sync(self, item: Item, cursor: str | None, count: int) -> dict:
        options = {"count": count}
        if cursor is not None:
            options["cursor"] = cursor
        data = self.post("/transactions/sync", item, **options)
        if data["has_more"] and data["next_cursor"] == (cursor or ""):
            raise UpstreamError("INVALID_UPSTREAM_RESPONSE")
        return {
            "item_id": item.item_id,
            "added": [transaction(tx, item) for tx in data["added"]],
            "modified": [transaction(tx, item) for tx in data["modified"]],
            "removed": [
                {
                    "item_id": item.item_id,
                    "transaction_id": tx["transaction_id"],
                    "account_id": tx.get("account_id"),
                }
                for tx in data["removed"]
            ],
            "accounts": [account(a, item) for a in data.get("accounts", [])],
            "next_cursor": data["next_cursor"],
            "has_more": data["has_more"],
            "transactions_update_status": data.get("transactions_update_status"),
        }


def transaction(tx: dict, item: Item) -> dict:
    """Keep Plaid sign conventions and pending→posted IDs; never invent currency/categories."""
    return {
        "item_id": item.item_id,
        "transaction_id": tx["transaction_id"],
        "account_id": tx["account_id"],
        "date": tx["date"],
        "authorized_date": tx.get("authorized_date"),
        "datetime": tx.get("datetime"),
        "authorized_datetime": tx.get("authorized_datetime"),
        "name": tx["name"],
        "merchant_name": tx.get("merchant_name"),
        "amount": tx["amount"],
        "currency": tx.get("iso_currency_code"),
        "unofficial_currency_code": tx.get("unofficial_currency_code"),
        "pending": tx["pending"],
        "pending_transaction_id": tx.get("pending_transaction_id"),
        "category": tx.get("category") or [],
        "personal_finance_category": tx.get("personal_finance_category"),
        "payment_channel": tx.get("payment_channel"),
    }


def account(a: dict, item: Item) -> dict:
    b = a.get("balances") or {}
    return {
        "item_id": item.item_id,
        "account_id": a["account_id"],
        "institution": item.institution_name,
        "name": a["name"],
        "official_name": a.get("official_name"),
        "type": a.get("type"),
        "subtype": a.get("subtype"),
        "mask": a.get("mask"),
        "balance": {
            "current": b.get("current"),
            "available": b.get("available"),
            "limit": b.get("limit"),
            "currency": b.get("iso_currency_code"),
            "unofficial_currency_code": b.get("unofficial_currency_code"),
        },
    }
