"""Free-form credential redaction for diagnostic and error text.

Pulumi cloud-provider errors regularly include credentials in stderr (AWS
access keys echoed back in misconfiguration errors, Azure connection
strings in 'cannot connect' messages, GCP service account JSON fragments,
bearer tokens, etc.). tlumi displays this text to the user and emits it
in JSON envelopes. This module scrubs the high-confidence patterns
before display.

This is separate from tlumi.sanitize: sanitize.py operates on Pulumi's
structured sentinels in state inputs/outputs; redact.py operates on
unstructured stderr/diagnostic strings.

The patterns are conservative on purpose. False positives (redacting a
substring that wasn't really a credential) are visible (the user sees
``***``) but reversible (the original info usually appears elsewhere in
the error). False negatives (leaking a credential) are silent and
permanent, so the patterns lean toward catching known credential shapes
rather than guessing aggressively.
"""

from __future__ import annotations

import re
from typing import Final

REDACTED: Final = "***"

# AWS access-key IDs: documented as 20 chars starting with AKIA/ASIA/AIDA/AGPA/AROA.
_AWS_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA|AIDA|AGPA|AROA|ANPA|ANVA)[A-Z0-9]{16}\b")

# JWT-shaped tokens: three base64url segments separated by '.', each at least
# 8 chars (signature is the third). Header.payload.signature; 8-char floor
# avoids matching short dotted identifiers.
# Segment length is intentionally unbounded: Azure AD / Entra access-token
# payloads with group/role claims routinely exceed a couple thousand base64url
# chars, and a per-segment cap would let an oversized real token escape masking
# and leak in full. The ReDoS trap is instead closed at the anchors, not with a
# length cap: '-' is inside the class but is not a regex word char, so a plain
# \b would open a valid start at every hyphen transition in text like
# 'a-a-a-...', and an unbounded {8,} at each of those O(n) starts would scan the
# rest of the run, giving O(n^2). The negative lookbehind/lookahead
# (?<![A-Za-z0-9_-]) / (?![A-Za-z0-9_-]) restrict a match to true run
# boundaries (start of a contiguous class-char run), so there is at most one
# start per run and the scan stays linear even with unbounded segments.
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)

# Generic key=value / key:value pairs, where key looks credential-bearing.
# Tolerates whitespace, an optional quote on the *key* so JSON / quoted-key
# forms like `"password": "x"` match (cloud providers return JSON error
# bodies), and quoted or bare values. A quoted value is consumed to its
# closing quote so multi-word secrets do not leak after the first space; a
# bare value stops at the next delimiter so embedded errors that mention
# `?token=abc&other=...` only mask the token.
_KV = re.compile(
    r"""(?xi)
    (?P<key>
        password
        | passwd
        | secret[_-]?access[_-]?key
        | secret(?:[_-]?key)?
        | api[_-]?key
        | auth[_-]?token
        | access[_-]?token
        | bearer[_-]?token
        | refresh[_-]?token
        | id[_-]?token
        | session[_-]?token
        | private[_-]?key
        | client[_-]?secret
        | account[_-]?key
        | shared[_-]?access[_-]?key
        | sas
        | token
    )
    ["']?\s*[:=]\s*
    (?:
        "[^"]*"
        | '[^']*'
        | [^\s"',;&]+
    )
    """
)

# Authorization-header style: "Authorization: Bearer xxx", "Authorization: Basic yyy".
_AUTH_HEADER = re.compile(
    r"(?i)\b(?P<scheme>Bearer|Basic|Token|Digest|API[-_]?Key)\s+(?P<value>[A-Za-z0-9._\-/=+]{8,})"
)

# Connection-string fragments: "Password=foo;User=bar" style. Case-insensitive,
# delimited by ';'. A quoted value is consumed to its closing quote so a
# multi-word secret does not leak past the first space; a bare value stops at
# the next ';' or whitespace.
_CONNSTR = re.compile(
    r"""(?xi)
    \b
    (?P<key>Password|Pwd|AccountKey|SharedAccessKey|SecretAccessKey)
    \s*=\s*
    (?P<value>"[^"]*"|'[^']*'|[^;\s]+)
    """
)

# GCP service account JSON has "private_key_id" and "private_key" fields.
# The inner span and the marker word runs are length-bounded: an unbounded lazy
# '.*?' between BEGIN and END lets every BEGIN marker (when no matching END
# follows) lazily expand to end-of-input before failing, giving O(n^2) on text
# with repeated BEGIN markers. A PEM key body is a few KB at most, so 8192 chars
# is generous while keeping the scan linear on hostile stderr.
_GCP_PRIVATE_KEY = re.compile(
    r"(?is)-----BEGIN [A-Z ]{0,32}?PRIVATE KEY-----.{0,8192}?-----END [A-Z ]{0,32}?PRIVATE KEY-----"
)

# Credentials embedded in a URL netloc: scheme://user:password@host. Backend
# URLs and provider errors echo these (e.g. s3://key:secret@host). Only the
# password segment is masked; the username/host stay visible for diagnosis.
# The username may be empty: redis://:pw@host and token-auth HTTPS remotes use
# the empty-user form, and _mask_backend_url in workspace.py already masks it.
# Every quantifier is bounded: an unbounded scheme/user/pw causes O(n^2)
# backtracking (a multi-second hang) on large provider stderr that has no '@'.
# Real schemes/credentials sit well under these caps; the user class excludes
# ':' so the {0,256} floor adds no backtracking paths.
_URL_USERINFO = re.compile(
    r"(?i)(?P<scheme>[a-z][a-z0-9+.\-]{0,31}://)"
    r"(?P<user>[^:/?#\s@]{0,256}):(?P<pw>[^@/?#\s]{1,256})@"
)

# Bare-userinfo credential URLs: scheme://TOKEN@host, with no ':' in the
# userinfo. S3-compatible/token-auth remotes place the whole credential in the
# userinfo, and _mask_backend_url in workspace.py already classifies a lone
# userinfo as a credential ("backend URLs almost never use bare usernames for
# identification"). _URL_USERINFO above (which requires a ':') runs first and
# consumes the user:pass form; this pattern (userinfo excludes ':') then catches
# the remaining bare-token shape. Quantifiers bounded to keep the scan linear.
_URL_BARE_USERINFO = re.compile(
    r"(?i)(?P<scheme>[a-z][a-z0-9+.\-]{0,31}://)(?P<token>[^:/?#\s@]{1,256})@"
)

# Azure SAS query params: ?sv=...&se=...&sig=... The signature (sig) is the
# credential itself; sv/se are masked too for parity with the backend-URL
# masking in workspace.py (_SENSITIVE_QUERY_KEYS), which classifies all three
# as sensitive. Anchored on a query delimiter so unrelated two-letter keys in
# free-form prose are not masked. The signature alternative allows a bounded
# prefix so AWS/GCS presigned-URL keys (X-Amz-Signature, X-Goog-Signature) are
# caught too -- their signature is the replayable credential of the presigned URL.
_SAS_QUERY = re.compile(
    r"(?i)(?P<delim>[?&])(?P<key>[a-z0-9_-]{0,64}signature|sig|sv|se)=(?P<value>[^&\s\"']{1,512})"
)


def _sub_url_userinfo(match: re.Match[str]) -> str:
    return f"{match.group('scheme')}{match.group('user')}:{REDACTED}@"


def _sub_url_bare_userinfo(match: re.Match[str]) -> str:
    return f"{match.group('scheme')}{REDACTED}@"


def _sub_sas(match: re.Match[str]) -> str:
    return f"{match.group('delim')}{match.group('key')}={REDACTED}"


def _sub_kv(match: re.Match[str]) -> str:
    return f"{match.group('key')}={REDACTED}"


def _sub_auth_header(match: re.Match[str]) -> str:
    return f"{match.group('scheme')} {REDACTED}"


def _sub_connstr(match: re.Match[str]) -> str:
    return f"{match.group('key')}={REDACTED}"


def redact_text(text: str) -> str:
    """Redact common credential patterns from free-form diagnostic text.

    Returns the input unchanged if no patterns match. Patterns are applied
    in a fixed order; the more specific patterns run first so generic
    key=value matching does not partially mask a JWT or PEM block.
    """
    if not text:
        return text
    out = _GCP_PRIVATE_KEY.sub(REDACTED, text)
    out = _AWS_ACCESS_KEY.sub(REDACTED, out)
    out = _JWT.sub(REDACTED, out)
    out = _URL_USERINFO.sub(_sub_url_userinfo, out)
    out = _URL_BARE_USERINFO.sub(_sub_url_bare_userinfo, out)
    out = _AUTH_HEADER.sub(_sub_auth_header, out)
    out = _CONNSTR.sub(_sub_connstr, out)
    out = _SAS_QUERY.sub(_sub_sas, out)
    out = _KV.sub(_sub_kv, out)
    return out
