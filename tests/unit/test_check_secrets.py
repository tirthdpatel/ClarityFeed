"""Tests for scripts/check_secrets.py.

A secret scanner has two failure modes and both are bad:

* **False negatives** let a credential through — the obvious harm.
* **False positives** are subtler and, in practice, more corrosive: a hook
  that fires on ``.env.example`` gets bypassed with ``--no-verify`` within a
  week, and then it protects nothing at all.

Both directions are tested here.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from check_secrets import scan_text  # noqa: E402


REAL_SECRETS = [
    pytest.param(
        "DATABASE_URL=postgresql://postgres:Tr0ub4dor3xK9@db.abcdefgh.supabase.co:5432/postgres",
        "postgres-url-with-password",
        id="supabase-direct-connection",
    ),
    pytest.param(
        'url = "postgres://postgres.projref:s3cretpw99@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"',
        "postgres-url-with-password",
        id="supabase-session-pooler",
    ),
    pytest.param(
        "KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.sig_value_x",
        "supabase-jwt-key",
        id="supabase-service-role-jwt",
    ),
    pytest.param(
        "GROQ_API_KEY=gsk_abcdefghij1234567890ABCDEFGHIJ", "groq-api-key", id="groq"
    ),
    pytest.param(
        "HF_API_TOKEN=hf_abcdefghij1234567890ABCDEFGHIJ", "huggingface-token", id="hf"
    ),
    pytest.param(
        "GEMINI_API_KEY=AIzaSyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q",
        "google-api-key",
        id="google",
    ),
    pytest.param('k = "AKIAIOSFODNN7EXAMPLE"', "aws-access-key", id="aws"),
    pytest.param(
        "-----BEGIN RSA PRIVATE KEY-----", "private-key-block", id="private-key"
    ),
    pytest.param(
        'password = "hunter2isnotgoodenough"', "generic-assigned-secret", id="generic"
    ),
]

PLACEHOLDERS_AND_NORMAL_CODE = [
    pytest.param(
        "postgresql://postgres:[YOUR-PASSWORD]@db.abcdefgh.supabase.co:5432/postgres",
        id="supabase-template-from-dashboard",
    ),
    pytest.param(
        "postgres://postgres.[PROJECT-REF]:[YOUR-PASSWORD]@aws-[REGION].pooler.supabase.com:5432/postgres",
        id="pooler-template",
    ),
    pytest.param(
        "DATABASE_URL=postgresql://user:password@ep-xxx.aws.neon.tech/neondb",
        id="env-example-placeholder",
    ),
    pytest.param("GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxx", id="groq-placeholder"),
    pytest.param(
        'os.environ.setdefault("INTERNAL_SECRET", "test_secret_123")', id="test-fixture"
    ),
    pytest.param(
        'INTERNAL_SECRET: str = "change_this_to_a_random_secret_string"',
        id="settings-default",
    ),
    pytest.param('DATABASE_URL: str = "sqlite:///./test.db"', id="sqlite-default"),
    pytest.param(
        "response = await client.post(self._hf_url, headers=self._headers)",
        id="ordinary-code",
    ),
]


@pytest.mark.parametrize("line,expected_rule", REAL_SECRETS)
def test_real_secrets_are_detected(line: str, expected_rule: str) -> None:
    findings = scan_text(line, "backend/some_module.py")
    assert findings, f"scanner missed a real secret: {expected_rule}"
    assert findings[0][1].name == expected_rule


@pytest.mark.parametrize("line", PLACEHOLDERS_AND_NORMAL_CODE)
def test_placeholders_do_not_trigger(line: str) -> None:
    findings = scan_text(line, "backend/some_module.py")
    assert not findings, f"false positive: {findings[0][1].name if findings else ''}"


def test_env_example_is_allowlisted() -> None:
    """.env.example documents the shape of secrets and must never trip."""
    line = "DATABASE_URL=postgresql://postgres:realpassword123@db.x.supabase.co:5432/postgres"
    assert not scan_text(line, ".env.example")
    # ...but the same line anywhere else must fire.
    assert scan_text(line, "config/settings.py")


def test_noqa_marker_suppresses() -> None:
    line = 'url = "postgres://u:realpassword99@host:5432/db"  # noqa: secret'
    assert not scan_text(line, "config/settings.py")
