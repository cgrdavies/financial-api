"""Environment-only configuration. Never print configuration values or validation input."""

import json
import os
from dataclasses import dataclass, field

from cryptography.fernet import Fernet, InvalidToken

HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}


@dataclass(frozen=True)
class Item:
    item_id: str
    access_token: str = field(repr=False)
    institution_name: str | None = None


@dataclass(frozen=True)
class Settings:
    api_token: str = field(repr=False)
    plaid_client_id: str = field(repr=False)
    plaid_secret: str = field(repr=False)
    plaid_environment: str
    items: tuple[Item, ...] = field(repr=False)

    @classmethod
    def from_env(cls) -> "Settings":
        def setting(name: str, default: str = "") -> str:
            # New names win; keep Spendy aliases so existing Dokploy credentials work.
            return os.environ.get(f"FINANCIAL_{name}", os.environ.get(f"SPENDY_{name}", default))

        api_token = setting("API_TOKEN")
        if len(api_token) < 32 or any(c.isspace() for c in api_token):
            raise ValueError(
                "FINANCIAL_API_TOKEN must be a random secret of at least 32 characters"
            )
        client_id, secret = setting("PLAID_CLIENT_ID"), setting("PLAID_SECRET")
        if not client_id or not secret:
            raise ValueError("Plaid client ID and secret are required")
        environment = setting("PLAID_ENVIRONMENT", "sandbox")
        if environment not in HOSTS:
            raise ValueError("Plaid environment must be sandbox or production")

        try:
            raw = json.loads(setting("PLAID_ITEMS_JSON", "[]"))
            if not isinstance(raw, list) or not 1 <= len(raw) <= 100:
                raise ValueError
            items = []
            for row in raw:
                if not isinstance(row, dict) or set(row) - {
                    "item_id",
                    "access_token",
                    "access_token_encrypted",
                    "institution_name",
                }:
                    raise ValueError
                item_id = row.get("item_id")
                plain = row.get("access_token")
                encrypted = row.get("access_token_encrypted")
                if not isinstance(item_id, str) or not item_id.strip() or len(item_id) > 200:
                    raise ValueError
                if ("access_token" in row) == ("access_token_encrypted" in row):
                    raise ValueError
                if encrypted:
                    plain = (
                        Fernet(setting("ENCRYPTION_KEY").encode())
                        .decrypt(encrypted.encode())
                        .decode()
                    )
                if not isinstance(plain, str) or not plain.strip():
                    raise ValueError
                institution = row.get("institution_name")
                if institution is not None and not isinstance(institution, str):
                    raise ValueError
                items.append(Item(item_id, plain, institution))
            if len({item.item_id for item in items}) != len(items):
                raise ValueError
        except (ValueError, TypeError, AttributeError, InvalidToken, UnicodeError):
            raise ValueError(
                "Invalid FINANCIAL_PLAID_ITEMS_JSON or encryption key: supply 1–100 unique items, "
                "each with item_id and exactly one access_token or access_token_encrypted"
            ) from None
        return cls(api_token, client_id, secret, environment, tuple(items))
