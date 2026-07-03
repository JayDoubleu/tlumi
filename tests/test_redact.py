"""Tests for tlumi.redact: credential redaction in free-form diagnostic text."""

from __future__ import annotations

from tlumi.redact import REDACTED, redact_text


def test_empty_string_unchanged():
    assert redact_text("") == ""


def test_no_credentials_unchanged():
    text = "Resource creation succeeded for bucket my-app-prod."
    assert redact_text(text) == text


def test_aws_access_key_id():
    out = redact_text("Error: invalid access key AKIAIOSFODNN7EXAMPLE")
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert REDACTED in out


def test_aws_temporary_access_key():
    out = redact_text("STS returned ASIA1234567890ABCDEF for the session.")
    assert "ASIA1234567890ABCDEF" not in out
    assert REDACTED in out


def test_password_kv_assignment():
    out = redact_text("DB connection failed: password=hunter2 host=db1")
    assert "hunter2" not in out
    assert "password=***" in out
    assert "host=db1" in out  # surrounding context preserved


def test_token_kv_assignment():
    out = redact_text("Request blocked: token=eyJabcd1234 refused")
    assert "eyJabcd1234" not in out
    assert "token=***" in out


def test_api_key_with_hyphen():
    out = redact_text("X-API-Key=sk_live_abcdefghijklmn rejected")
    assert "sk_live_abcdefghijklmn" not in out
    assert "***" in out


def test_bearer_token_in_authorization_header():
    out = redact_text("HTTP 401: Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.foo.bar")
    assert "eyJhbGciOiJIUzI1NiJ9" not in out
    assert "Bearer ***" in out


def test_basic_auth_redacted():
    out = redact_text("curl returned: Authorization: Basic dXNlcjpwYXNz")
    assert "dXNlcjpwYXNz" not in out
    assert "Basic ***" in out


def test_jwt_three_segments():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.SflKxwRJSMeKKF2QT4f"
    out = redact_text(f"Token mismatch: {jwt}")
    assert jwt not in out
    assert REDACTED in out


def test_jwt_large_payload_segment_masked():
    # Azure AD / Entra access-token JWTs with group claims routinely carry a
    # payload segment far beyond a couple thousand base64url chars. A bare token
    # echoed in provider stderr (no Bearer/key= framing) must still be masked;
    # _JWT must not silently give up on long segments.
    header = "eyJhbGciOiJIUzI1NiJ9"
    payload = "A" * 3000
    sig = "SflKxwRJSMeKKF2QT4f"
    jwt = f"{header}.{payload}.{sig}"
    out = redact_text(f"provider error: {jwt} failed")
    assert jwt not in out
    assert REDACTED in out


def test_short_dotted_identifier_not_treated_as_jwt():
    # 'foo.bar.baz' with each segment < 8 chars should not be redacted
    out = redact_text("Method foo.bar.baz returned 200.")
    assert "foo.bar.baz" in out


def test_connection_string_password():
    out = redact_text("Connection: Server=db1;Password=secret123;Database=app;")
    assert "secret123" not in out
    assert "Password=***" in out
    assert "Server=db1" in out
    assert "Database=app" in out


def test_azure_shared_access_key_in_connstr():
    out = redact_text("AccountKey=AbCdEf1234567890==;EndpointSuffix=core.windows.net")
    assert "AbCdEf1234567890==" not in out
    assert "AccountKey=***" in out


def test_gcp_pem_private_key_block():
    pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ...\n"
        "-----END PRIVATE KEY-----"
    )
    out = redact_text(f"Failed to parse service account: {pem} -- bad encoding")
    assert "MIIEvQIBADAN" not in out
    assert REDACTED in out


def test_url_query_token():
    out = redact_text("Cannot fetch https://api.example.com/v1?token=abc123def&id=42")
    assert "abc123def" not in out
    assert "token=***" in out
    assert "id=42" in out  # non-credential param preserved


def test_quoted_value():
    out = redact_text('Config error: password="my secret" not accepted')
    # A quoted value is consumed to its closing quote, so a multi-word secret
    # does not leak after the first space.
    assert "my secret" not in out
    assert "secret" not in out
    assert "password=***" in out


def test_json_quoted_key_password():
    # Cloud providers return JSON error bodies where the key itself is quoted.
    out = redact_text('{"error": "auth failed", "password": "hunter2"}')
    assert "hunter2" not in out
    assert REDACTED in out


def test_json_quoted_key_client_secret():
    out = redact_text('Response: {"client_secret": "abc123XYZ", "id": "42"}')
    assert "abc123XYZ" not in out
    assert REDACTED in out
    assert "42" in out  # non-credential field preserved


def test_json_quoted_key_sas_token():
    out = redact_text('{"sasToken": "sv=2021-06-08&sig=deadbeefsecret"}')
    assert "deadbeefsecret" not in out
    assert REDACTED in out


def test_json_quoted_key_access_token():
    out = redact_text('{"accessToken":"ya29.A0ARrdaM-secretvalue"}')
    assert "ya29.A0ARrdaM-secretvalue" not in out
    assert REDACTED in out


def test_secret_access_key_colon_style():
    # SecretAccessKey with a colon separator: not a ;-delimited connection
    # string and not '='-style, so only the quoted-key-aware _KV catches it.
    out = redact_text("Error: SecretAccessKey: wJalrXUtnFEMIKEXAMPLEKEY denied")
    assert "wJalrXUtnFEMIKEXAMPLEKEY" not in out
    assert REDACTED in out


def test_url_userinfo_masks_password():
    assert redact_text("backend s3://key:hunter2@host/bucket") == "backend s3://key:***@host/bucket"


def test_url_userinfo_masks_password_with_empty_username():
    """Empty-user credential URLs (redis://:pw@host, token-auth HTTPS) are masked."""
    out = redact_text("error connecting to redis://:supersecretpw@cache.example.com:6379")
    assert "supersecretpw" not in out
    assert "redis://:***@cache.example.com:6379" in out


def test_url_without_credentials_unchanged():
    """A plain URL with a port but no userinfo must not be mangled."""
    text = "fetched https://example.com:8080/path from upstream"
    assert redact_text(text) == text


def test_hyphenated_secret_key_flag_masked():
    """MinIO-style --secret-key=... in provider stderr is masked."""
    out = redact_text("minio --secret-key=SoSecret123 failed")
    assert "SoSecret123" not in out
    assert "secret-key=***" in out


def test_hyphenated_secret_key_colon_style_masked():
    out = redact_text("config: secret-key: SoSecret123")
    assert "SoSecret123" not in out
    assert REDACTED in out


def test_sas_query_params_masked():
    out = redact_text("blob https://a.blob.core.windows.net/c/b?sv=2021&sig=abc%2Bxyz&se=2025")
    assert "sig=***" in out
    assert "abc%2Bxyz" not in out


def test_redact_no_redos_on_large_input():
    """Bounded quantifiers: a large no-match input must not hang (ReDoS guard)."""
    import time

    big = "s3://" + "a" * 60000  # scheme prefix, long, no '@'
    start = time.monotonic()
    redact_text(big)
    assert time.monotonic() - start < 1.0, "redact_text should be linear, not O(n^2)"


def test_redact_no_redos_on_colon_heavy_input():
    """The empty-username form ({0,256}) must stay bounded on ':'-dense no-match text."""
    import time

    big = "s3://" + "a:b" * 20000  # many userinfo-shaped splits, no '@'
    start = time.monotonic()
    redact_text(big)
    assert time.monotonic() - start < 1.0, "redact_text should be linear, not O(n^2)"


def test_redact_no_redos_on_hyphen_alternating_input():
    """_JWT must be bounded: hyphen-alternating text creates a \\b boundary at every
    transition, and an unbounded {8,} quantifier backtracks O(n^2)."""
    import time

    big = "s3://" + "a-" * 50000  # hyphen every char: \b at each, no '.', no match
    start = time.monotonic()
    redact_text(big)
    assert time.monotonic() - start < 1.0, "redact_text _JWT should be linear, not O(n^2)"


def test_redact_no_redos_on_repeated_begin_markers():
    """_GCP_PRIVATE_KEY must be bounded: repeated BEGIN markers with no END make the
    lazy '.*?' expand to end-of-string at each marker, giving O(n^2)."""
    import time

    big = "-----BEGIN PRIVATE KEY----- " * 8000  # repeated BEGIN, never an END
    start = time.monotonic()
    redact_text(big)
    assert time.monotonic() - start < 1.0, "redact_text _GCP_PRIVATE_KEY should be linear"


def test_url_userinfo_masks_bare_token():
    """A bare token in the userinfo (scheme://TOKEN@host, no colon) is masked, matching
    _mask_backend_url which classifies lone userinfo as a credential."""
    out = redact_text(
        "error open bucket s3://GHSAT0AAAABBBBCCCCDDDD@minio.internal:9000/tlumi-state: 403"
    )
    assert "GHSAT0AAAABBBBCCCCDDDD" not in out
    assert "s3://***@minio.internal:9000/tlumi-state" in out


def test_url_userinfo_bare_token_does_not_mask_plain_url_with_at_in_path():
    """A URL with no userinfo '@' (the '@' is elsewhere, gated by '/') is untouched."""
    text = "fetched https://example.com/u/a@b from upstream"
    assert redact_text(text) == text


def test_aws_presigned_signature_masked():
    """X-Amz-Signature (the replayable credential of a presigned S3 URL) is masked."""
    out = redact_text(
        "403 https://b.s3.amazonaws.com/k?X-Amz-Credential=cred&X-Amz-Signature=deadbeefcafe1234&x=1"
    )
    assert "deadbeefcafe1234" not in out
    assert "X-Amz-Signature=***" in out
    assert "x=1" in out


def test_gcs_presigned_signature_masked():
    """X-Goog-Signature (presigned GCS URL signature) is masked."""
    out = redact_text(
        "error https://storage.googleapis.com/b/o?X-Goog-Signature=abc123secretsig&x=1"
    )
    assert "abc123secretsig" not in out
    assert "X-Goog-Signature=***" in out
