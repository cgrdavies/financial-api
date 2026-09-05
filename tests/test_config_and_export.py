import base64
import importlib.util
import json
import os
import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from financial_api.config import Settings
from financial_api.main import create_app


@pytest.fixture
def env(monkeypatch):
    for name in os.environ:
        if name.startswith(("FINANCIAL_", "SPENDY_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FINANCIAL_API_TOKEN", "fake-random-token-" + "a" * 32)
    monkeypatch.setenv("FINANCIAL_PLAID_CLIENT_ID", "client-example")
    monkeypatch.setenv("FINANCIAL_PLAID_SECRET", "secret-example")
    monkeypatch.setenv(
        "FINANCIAL_PLAID_ITEMS_JSON",
        json.dumps([{"item_id": "item-example", "access_token": "access-example"}]),
    )
    return monkeypatch


def test_plaintext_config_and_safe_repr(env):
    s = Settings.from_env()
    assert s.plaid_environment == "sandbox"
    assert s.items[0].access_token == "access-example"
    assert "access-example" not in repr(s)
    assert "access-example" not in repr(s.items[0])
    assert "secret-example" not in repr(s)


def test_spendy_aliases_and_encrypted_tokens(env):
    key = Fernet.generate_key()
    env.setenv("SPENDY_ENCRYPTION_KEY", key.decode())
    env.delenv("FINANCIAL_PLAID_SECRET")
    env.setenv("SPENDY_PLAID_SECRET", "old-secret")
    env.setenv(
        "FINANCIAL_PLAID_ITEMS_JSON",
        json.dumps(
            [
                {
                    "item_id": "item-example",
                    "access_token_encrypted": Fernet(key).encrypt(b"access-example").decode(),
                }
            ]
        ),
    )
    s = Settings.from_env()
    assert s.items[0].access_token == "access-example"
    assert s.plaid_secret == "old-secret"
    env.setenv("FINANCIAL_PLAID_SECRET", "new-secret")
    assert Settings.from_env().plaid_secret == "new-secret"


@pytest.mark.parametrize(
    "value",
    [
        "not-json",
        "{}",
        "[]",
        "[1]",
        '[{"item_id":"item", "access_token":"access-example", "extra":"secret"}]',
        '[{"item_id":"item", "access_token":"access-example", "access_token_encrypted":"secret"}]',
        '[{"item_id":"item", "access_token_encrypted":"secret"}]',
        '[{"item_id":"item", "access_token":42}]',
        '[{"item_id":"", "access_token":"access-example"}]',
        '[{"item_id":"item", "access_token":"a"},{"item_id":"item", "access_token":"b"}]',
    ],
)
def test_bad_items_rejected_without_secret_echo(env, value):
    env.setenv("FINANCIAL_PLAID_ITEMS_JSON", value)
    with pytest.raises(ValueError) as exc:
        Settings.from_env()
    assert "access-example" not in str(exc.value)


@pytest.mark.parametrize(
    "key,value",
    [
        ("FINANCIAL_API_TOKEN", "change-me"),
        ("FINANCIAL_PLAID_CLIENT_ID", ""),
        ("FINANCIAL_PLAID_SECRET", ""),
        ("FINANCIAL_PLAID_ENVIRONMENT", "development"),
        ("FINANCIAL_PLAID_ENVIRONMENT", "typo"),
    ],
)
def test_invalid_config_fails_closed(env, key, value):
    env.setenv(key, value)
    with pytest.raises(ValueError):
        Settings.from_env()
    with pytest.raises(RuntimeError, match="Invalid financial-api configuration"):
        with TestClient(create_app()):
            pass


@pytest.mark.parametrize("output_format", ["json", "base64"])
def test_export_is_encrypted_only_read_only_and_rejects_overwrite(tmp_path, env, output_format):
    script = Path(__file__).parents[1] / "scripts" / "export_spendy_items.py"
    spec = importlib.util.spec_from_file_location("exporter", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db = tmp_path / "source.sqlite"
    key = Fernet.generate_key()
    token = Fernet(key).encrypt(b"access-example").decode()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE plaid_items (item_id TEXT, access_token_encrypted TEXT, "
            "institution_name TEXT, cursor TEXT)"
        )
        conn.execute(
            "INSERT INTO plaid_items VALUES (?, ?, ?, ?)",
            ("item-example", token, "Example Bank", "do-not-export-cursor"),
        )
    original = db.read_bytes()
    out = tmp_path / "export.json"
    assert module.export(db, out, output_format) == 1
    assert out.stat().st_mode & 0o777 == 0o600
    assert "access-example" not in out.read_text()
    assert "do-not-export-cursor" not in out.read_text()
    payload = out.read_text()
    if output_format == "base64":
        assert len(payload.splitlines()) == 1
        env.setenv("FINANCIAL_PLAID_ITEMS_B64", payload.strip())
        env.setenv("FINANCIAL_ENCRYPTION_KEY", key.decode())
        assert Settings.from_env().items[0].access_token == "access-example"
        payload = base64.b64decode(payload.strip(), validate=True).decode()
    assert json.loads(payload)[0]["access_token_encrypted"] == token
    assert db.read_bytes() == original
    with pytest.raises(FileExistsError):
        module.export(db, out, output_format)


def test_config_ignores_cwd_dotenv(env, tmp_path):
    (tmp_path / ".env").write_text("FINANCIAL_PLAID_SECRET=must-not-load\n")
    env.chdir(tmp_path)
    assert Settings.from_env().plaid_secret == "secret-example"


@pytest.mark.parametrize("variable", ["FINANCIAL_PLAID_ITEMS_B64", "SPENDY_PLAID_ITEMS_B64"])
def test_base64_configuration_overrides_json(env, variable):
    payload = json.dumps(
        [
            {
                "item_id": "base64-item",
                "access_token": "base64-access",
                "institution_name": "Example ü Bank",
            }
        ]
    ).encode()
    env.setenv(variable, base64.b64encode(payload).decode())
    s = Settings.from_env()
    assert s.items[0].item_id == "base64-item"
    assert s.items[0].access_token == "base64-access"
    assert s.items[0].institution_name == "Example ü Bank"


@pytest.mark.parametrize(
    "encoded", ["%%%secret-do-not-echo", " ", "W10", "////", "bm90LWpzb24=", "e30=", "W10="]
)
def test_invalid_base64_fails_closed_despite_valid_json(env, encoded):
    env.setenv("FINANCIAL_PLAID_ITEMS_B64", encoded)
    with pytest.raises(ValueError) as exc:
        Settings.from_env()
    assert "secret-do-not-echo" not in str(exc.value)
    with pytest.raises(RuntimeError, match="Invalid financial-api configuration"):
        with TestClient(create_app()):
            pass


def test_empty_base64_falls_back_to_json_and_financial_alias_wins(env):
    env.setenv("SPENDY_PLAID_ITEMS_B64", "invalid-spendy-value")
    env.setenv("FINANCIAL_PLAID_ITEMS_B64", "")
    assert Settings.from_env().items[0].access_token == "access-example"
