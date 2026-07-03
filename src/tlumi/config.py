"""Parse and manage tlumi.yaml project configuration."""

from __future__ import annotations

import difflib
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from tlumi.errors import ConfigError, ProjectNotFoundError

_log = logging.getLogger(__name__)

CONFIG_FILE = "tlumi.yaml"
TLUMI_DIR = ".tlumi"
STATE_DIR = "state"
CACHE_DIR = "cache"
PULUMI_HOME_DIR = "pulumi_home"
VENV_DIR = "venv"
DEFAULT_ENTRY = "infra.py"
DEFAULT_STACK = "default"


@dataclass(frozen=True, slots=True)
class BackendConfig:
    url: str | None = None

    def resolved_url(self, project_dir: Path) -> str:
        """Return the backend URL, defaulting to local file state."""
        if self.url:
            return self.url
        return f"file://{project_dir / TLUMI_DIR / STATE_DIR}"


@dataclass(frozen=True, slots=True)
class SecretsConfig:
    warn_unencrypted: bool = True
    allow_unencrypted: bool = False


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """Immutable project configuration.

    Frozen so callers cannot accidentally mutate validated state and bypass
    ``__post_init__`` invariants. Variables come in via ``merge_variables``
    which returns a new instance instead of mutating in place. The
    ``variables`` dict itself is still mutable Python-wise (frozen
    dataclasses freeze attribute assignment, not the values), but the only
    sanctioned modification path is to construct a new ProjectConfig and
    revalidate.
    """

    name: str
    project_dir: Path
    entry: str = DEFAULT_ENTRY
    backend: BackendConfig = field(default_factory=BackendConfig)
    secrets: SecretsConfig = field(default_factory=SecretsConfig)
    variables: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Duplicates validation from load_config() to catch programmatic
        # construction that bypasses YAML loading.
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9_-]*\Z", self.name):
            raise ConfigError(
                f"Invalid project name: '{self.name}'",
                hint="Name must start with a letter and contain only"
                " letters, digits, hyphens, and underscores.",
            )
        if not self.project_dir.is_absolute():
            raise ConfigError(
                f"project_dir must be an absolute path, got '{self.project_dir}'",
            )
        if not self.entry:
            raise ConfigError("Entry file path must not be empty.")
        if ".." in self.entry:
            raise ConfigError(
                f"Entry path '{self.entry}' contains path traversal.",
                hint="The 'project.entry' path must not contain '..'.",
            )
        if self.entry.startswith("/"):
            raise ConfigError(
                f"Entry path '{self.entry}' is an absolute path.",
                hint="The 'project.entry' path must be relative to the project directory.",
            )
        if any(":" in k for k in self.variables):
            raise ConfigError(
                "Variable keys must not contain ':'.",
                hint="Colons are reserved for Pulumi provider config namespaces.",
            )

    @property
    def tlumi_dir(self) -> Path:
        return self.project_dir / TLUMI_DIR

    @property
    def state_dir(self) -> Path:
        return self.project_dir / TLUMI_DIR / STATE_DIR

    @property
    def cache_dir(self) -> Path:
        return self.project_dir / TLUMI_DIR / CACHE_DIR

    @property
    def pulumi_home(self) -> Path:
        return self.cache_dir / PULUMI_HOME_DIR

    @property
    def venv_dir(self) -> Path:
        return self.cache_dir / VENV_DIR

    @property
    def venv_python(self) -> Path:
        return self.venv_dir / "bin" / "python"

    @property
    def entry_path(self) -> Path:
        return self.project_dir / self.entry

    def backend_url(self) -> str:
        return self.backend.resolved_url(self.project_dir)


_KNOWN_TOP_KEYS = ("project", "backend", "secrets", "variables")
_KNOWN_PROJECT_KEYS = ("name", "entry")
_KNOWN_BACKEND_KEYS = ("url",)
_KNOWN_SECRETS_KEYS = ("allow_unencrypted", "warn_unencrypted")

_YAML_INT_TAG = "tag:yaml.org,2002:int"
_NON_DECIMAL_INT_TAG = "!tlumi/non-decimal-int"
# Round-tripping decimal ints only: these are the sole int forms whose text
# survives a parse-then-stringify round trip (str(int(x)) == x). Everything
# else PyYAML's YAML 1.1 int resolver accepts (leading-zero octal, 0x/0b,
# underscore separators, sexagesimal, plus the decimal-but-lossy '+7' and
# '-0' forms whose sign stringification drops) is remapped to
# _NON_DECIMAL_INT_TAG below.
_DECIMAL_INT_RE = re.compile(r"^(?:0|-?[1-9][0-9]*)\Z")

_YAML_BOOL_TAG = "tag:yaml.org,2002:bool"
_NON_CANONICAL_BOOL_TAG = "!tlumi/non-canonical-bool"
# YAML 1.1 also resolves yes/no/on/off (and case variants) to booleans, so an
# unquoted ISO country code ``NO`` or literal ``on`` is silently rewritten to a
# different string ('false'/'true') -- the classic "Norway problem". Only the
# true/false word forms round-trip to the documented lowercase spelling; the
# rest are remapped to _NON_CANONICAL_BOOL_TAG so the coercion step can reject
# them with a quote hint.
_CANONICAL_BOOL_RE = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)\Z")

_YAML_FLOAT_TAG = "tag:yaml.org,2002:float"
_FLOAT_TEXT_TAG = "!tlumi/float-text"
_YAML_TIMESTAMP_TAG = "tag:yaml.org,2002:timestamp"
_TIMESTAMP_TEXT_TAG = "!tlumi/timestamp-text"
# Floats and timestamps are always rejected (both are lossy-rewrite classes:
# ``1.10`` -> 1.1, ``2026-07-02T10:00:00Z`` -> a datetime whose str() drops the
# 'T'/'Z' form), so every match is retagged to a raw-text marker rather than
# split into a round-tripping subset. Retagging timestamps also stops PyYAML's
# timestamp constructor from raising a bare ValueError on out-of-range implicit
# dates (e.g. ``2026-13-01``).


class _NonDecimalIntText(str):
    """Raw text of a YAML 1.1 int form that does not round-trip.

    PyYAML silently resolves ``0777`` to 511 and ``1:30`` to 90, the same
    lossy-rewrite class that gets floats rejected in ``_coerce_variable_value``.
    Despite the name, this also carries the decimal-but-lossy forms ``+7``
    and ``-0``, whose sign stringification drops (``+7`` would silently
    become ``7``). The strict loader preserves the original text in this
    marker type so the coercion step can reject it with a hint that quotes
    the value the user actually wrote. Subclassing ``str`` keeps non-variable
    config fields (which validate against ``str``) behaving sensibly for
    these scalars.
    """

    __slots__ = ()


class _NonCanonicalBoolText(str):
    """Raw text of a YAML 1.1 bool spelling that is not canonical true/false.

    PyYAML resolves ``yes``/``no``/``on``/``off`` (and case variants) to
    booleans, so an ISO country code ``NO`` or a literal ``on`` would silently
    become 'false'/'true'. The strict loader preserves the original text here so
    ``_coerce_variable_value`` can reject it with a hint quoting what the user
    wrote. Subclassing ``str`` keeps non-variable config fields sensible.
    """

    __slots__ = ()


class _FloatText(str):
    """Raw text of a YAML float scalar.

    Floats are always rejected because YAML silently rewrites ``1.10`` to
    ``1.1``. Carrying the raw source text (rather than the parsed float) lets the
    rejection quote what the user actually wrote instead of the corrupted value,
    matching the ``_NonDecimalIntText`` treatment on the int side.
    """

    __slots__ = ()


class _TimestampText(str):
    """Raw text of a YAML timestamp scalar.

    PyYAML resolves ``2026-07-02T10:00:00Z`` to a datetime whose str() drops the
    'T'/'Z' form and zero-pads fractional seconds. Preserving the raw text lets
    the coercion step reject it with a hint quoting the exact source, and it also
    keeps out-of-range implicit dates (month 13) from raising a bare ValueError
    inside PyYAML's timestamp constructor.
    """

    __slots__ = ()


class _StrictIntLoader(yaml.SafeLoader):
    """SafeLoader that refuses to lossily rewrite int scalars.

    Identical to ``yaml.SafeLoader`` except that only round-tripping decimal
    scalars (``_DECIMAL_INT_RE``) resolve to ints; the remaining YAML 1.1 int
    forms resolve to ``_NonDecimalIntText`` carrying the raw text. Resolver
    order for all other tags is preserved exactly.
    """


def _construct_non_decimal_int(
    loader: yaml.SafeLoader, node: yaml.ScalarNode
) -> _NonDecimalIntText:
    return _NonDecimalIntText(node.value)


def _construct_non_canonical_bool(
    loader: yaml.SafeLoader, node: yaml.ScalarNode
) -> _NonCanonicalBoolText:
    return _NonCanonicalBoolText(node.value)


def _construct_float_text(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> _FloatText:
    return _FloatText(node.value)


def _construct_timestamp_text(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> _TimestampText:
    return _TimestampText(node.value)


_StrictIntLoader.add_constructor(_NON_DECIMAL_INT_TAG, _construct_non_decimal_int)
_StrictIntLoader.add_constructor(_NON_CANONICAL_BOOL_TAG, _construct_non_canonical_bool)
_StrictIntLoader.add_constructor(_FLOAT_TEXT_TAG, _construct_float_text)
_StrictIntLoader.add_constructor(_TIMESTAMP_TEXT_TAG, _construct_timestamp_text)


def _build_strict_resolvers() -> dict[str | None, list[tuple[str, re.Pattern[str]]]]:
    """Copy SafeLoader's implicit resolvers, hardening the lossy YAML 1.1 rules.

    Four scalar resolvers whose implicit coercion silently rewrites the source
    text are split or retagged so ``_coerce_variable_value`` sees a raw-text
    marker it can reject:

    * int: only round-tripping decimals (``_DECIMAL_INT_RE``) stay ints; the
      remaining forms (octal, hex, sexagesimal, ``+7``) retag to
      ``_NON_DECIMAL_INT_TAG``.
    * bool: only ``true``/``false`` word forms stay bools; yes/no/on/off retag to
      ``_NON_CANONICAL_BOOL_TAG``.
    * float and timestamp: every match retags to a raw-text tag (both are always
      rejected, so no round-tripping subset needs to stay parsed).

    Resolver order is preserved exactly, so anything the stock loader matched
    still hits one of the rebuilt entries.
    """
    resolvers: dict[str | None, list[tuple[str, re.Pattern[str]]]] = {}
    for first_char, pairs in yaml.SafeLoader.yaml_implicit_resolvers.items():
        rebuilt = []
        for tag, regexp in pairs:
            if tag == _YAML_INT_TAG:
                rebuilt.append((_YAML_INT_TAG, _DECIMAL_INT_RE))
                rebuilt.append((_NON_DECIMAL_INT_TAG, regexp))
            elif tag == _YAML_BOOL_TAG:
                rebuilt.append((_YAML_BOOL_TAG, _CANONICAL_BOOL_RE))
                rebuilt.append((_NON_CANONICAL_BOOL_TAG, regexp))
            elif tag == _YAML_FLOAT_TAG:
                rebuilt.append((_FLOAT_TEXT_TAG, regexp))
            elif tag == _YAML_TIMESTAMP_TAG:
                rebuilt.append((_TIMESTAMP_TEXT_TAG, regexp))
            else:
                rebuilt.append((tag, regexp))
        resolvers[first_char] = rebuilt
    return resolvers


_StrictIntLoader.yaml_implicit_resolvers = _build_strict_resolvers()


def _load_yaml(text: str) -> object:
    """Parse YAML with the strict int loader (used for tlumi.yaml and var files)."""
    # _StrictIntLoader subclasses yaml.SafeLoader, so this is exactly as safe
    # as yaml.safe_load(); bandit only pattern-matches the yaml.load call.
    return yaml.load(text, Loader=_StrictIntLoader)  # nosec B506


_STRICT_MARKERS = (_NonCanonicalBoolText, _NonDecimalIntText, _FloatText, _TimestampText)


def _canonical_value(value: object) -> object:
    """Undo the strict loader's raw-text markers for non-variable config fields.

    The strict loader deliberately surfaces lossy YAML 1.1 scalars
    (``yes``/``no``/``on``/``off``, non-decimal ints, floats, timestamps) as
    str-subclass markers so ``_coerce_variable_value`` can reject them. Those
    markers are meant for variable *values* only. If one reaches a typed config
    field (``project.name``/``entry``, ``backend.url``, ``secrets.*``) it would
    either slip past an ``isinstance(str)`` check (the markers subclass ``str``)
    or surface the internal marker class name in a user-facing error. Re-resolve
    the raw text through the stock SafeLoader so those fields see the ordinary
    Python type the pre-strict-loader code produced (``yes`` -> True, ``0777``
    -> 511, ``1.10`` -> 1.1, a timestamp -> datetime), keeping their existing
    boolean/string validation intact. A scalar that cannot be reconstructed
    (e.g. an out-of-range implicit date) falls back to its raw text rather than
    raising, so this never reintroduces the bare-ValueError crash the strict
    loader was added to prevent.
    """
    if isinstance(value, _STRICT_MARKERS):
        try:
            return yaml.safe_load(str(value))
        except (yaml.YAMLError, ValueError, RecursionError):
            return str(value)
    return value


def _warn_unknown_keys(mapping: dict, known: tuple[str, ...], section: str) -> None:
    """Warn (not fail) about unrecognized keys, suggesting the closest known key.

    A misspelled key (``vars:`` for ``variables:``, ``secret:`` for ``secrets:``)
    is otherwise silently ignored, so the setting never takes effect and
    ``tlumi validate`` still reports success. Warning keeps tlumi.yaml forward
    compatible (a future key does not hard-fail an older tlumi) while making the
    mistake visible. The notice is routed to stderr by the CLI handler.
    """
    for key in mapping:
        if key not in known:
            suggestion = difflib.get_close_matches(str(key), known, n=1)
            hint = f" (did you mean '{suggestion[0]}'?)" if suggestion else ""
            _log.warning("Unknown key '%s' in %s of tlumi.yaml%s", key, section, hint)


def _coerce_variable_value(key: str, value: object, source: str) -> str:
    """Validate and stringify a variable value, rejecting lossy/ambiguous types.

    YAML coerces unquoted scalars before tlumi sees them. Booleans are mapped to
    lowercase 'true'/'false'; plain decimal ints stringify losslessly. Floats are
    rejected because YAML silently rewrites values like ``1.10`` to ``1.1`` (a
    version or identifier corrupted with no warning) -- the user must quote such
    values. Int forms whose text does not round-trip (``0777`` -> 511, ``1:30``
    -> 90, ``0x1A`` -> 26, ``+7`` -> 7, ``-0`` -> 0) are the same lossy-rewrite
    class; the strict loader preserves their raw text as ``_NonDecimalIntText``
    and they are rejected here with a quote hint. YAML 1.1 boolean keywords
    (``yes``/``no``/``on``/``off``, the "Norway problem") and timestamps
    (``2026-07-02T10:00:00Z`` reformatted) are the same class and are likewise
    rejected via their raw-text markers. Lists/dicts, bare/null values, and
    embedded NUL bytes (which would crash the Pulumi CLI subprocess) are
    rejected too.
    """
    if "\x00" in key:
        raise ConfigError(
            f"A variable key in {source} contains a NUL byte.",
            hint="Remove the embedded NUL (\\0); it cannot be passed to Pulumi.",
        )
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, _NonCanonicalBoolText):
        raise ConfigError(
            f"Variable '{key}' in {source} is an unquoted YAML boolean keyword ({value}); "
            "YAML 1.1 rewrites yes/no/on/off to true/false.",
            hint=f'Quote it to preserve the exact text, e.g. {key}: "{value}".',
        )
    if isinstance(value, (list, dict)):
        raise ConfigError(
            f"Variable '{key}' in {source} must be a scalar value, not {type(value).__name__}.",
        )
    if value is None:
        raise ConfigError(
            f"Variable '{key}' in {source} has no value.",
            hint='Remove the key or give it an explicit value (use "" for empty).',
        )
    if isinstance(value, _FloatText):
        raise ConfigError(
            f"Variable '{key}' in {source} is an unquoted decimal ({value}); "
            "YAML silently rewrites values like 1.10 to 1.1.",
            hint=f'Quote it to preserve the exact text, e.g. {key}: "{value}".',
        )
    if isinstance(value, float):
        raise ConfigError(
            f"Variable '{key}' in {source} is an unquoted decimal ({value!r}); "
            "YAML silently rewrites values like 1.10 to 1.1.",
            hint=f'Quote it to preserve the exact text, e.g. {key}: "{value!r}".',
        )
    if isinstance(value, _TimestampText):
        raise ConfigError(
            f"Variable '{key}' in {source} is an unquoted timestamp ({value}); "
            "YAML silently reformats dates (the 'T'/'Z' form and fractional seconds change).",
            hint=f'Quote it to preserve the exact text, e.g. {key}: "{value}".',
        )
    if isinstance(value, _NonDecimalIntText):
        raise ConfigError(
            f"Variable '{key}' in {source} is an unquoted number whose text does not "
            f"round-trip ({value}); YAML silently rewrites values like 0777 to 511, "
            "1:30 to 90, and +7 to 7.",
            hint=f'Quote it to preserve the exact text, e.g. {key}: "{value}".',
        )
    if isinstance(value, str) and "\x00" in value:
        raise ConfigError(
            f"Variable '{key}' in {source} contains a NUL byte.",
            hint="Remove the embedded NUL (\\0); it cannot be passed to Pulumi.",
        )
    return str(value)


def load_config(project_dir: Path | None = None) -> ProjectConfig:
    """Load tlumi.yaml from the given directory (default: cwd)."""
    if project_dir is None:
        project_dir = Path.cwd()
    project_dir = project_dir.resolve()

    config_path = project_dir / CONFIG_FILE
    if not config_path.exists():
        raise ProjectNotFoundError()
    if config_path.is_symlink():
        raise ConfigError(
            f"tlumi.yaml is a symlink to {config_path.resolve()}",
            hint="Remove the symlink. A malicious repository may have created it.",
        )

    try:
        raw = _load_yaml(config_path.read_text()) or {}
    except (yaml.YAMLError, OSError, UnicodeDecodeError, ValueError, RecursionError) as e:
        # PyYAML raises bare ValueError from its timestamp constructor (an
        # explicit "!!timestamp 2026-13-01") and RecursionError on deeply nested
        # documents; neither is a yaml.YAMLError, so widen the catch to keep them
        # from escaping as raw tracebacks (and breaking the --json contract).
        raise ConfigError(f"Cannot read tlumi.yaml: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigError(
            "tlumi.yaml must contain a YAML mapping at the top level.",
            hint="Check that tlumi.yaml starts with key-value pairs, not a list or scalar.",
        )

    _warn_unknown_keys(raw, _KNOWN_TOP_KEYS, "the top level")

    project = raw.get("project", {})
    if not isinstance(project, dict):
        raise ConfigError("'project' must be a mapping in tlumi.yaml.")
    _warn_unknown_keys(project, _KNOWN_PROJECT_KEYS, "'project'")

    name = _canonical_value(project.get("name"))
    if not name:
        raise ConfigError(
            "Missing 'project.name' in tlumi.yaml.",
            hint="Add a 'project.name' field to your tlumi.yaml.",
        )
    if not isinstance(name, str):
        raise ConfigError(
            f"'project.name' must be a string, got {type(name).__name__}.",
            hint="Ensure 'project.name' is not a number or boolean in tlumi.yaml.",
        )

    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_-]*\Z", name):
        raise ConfigError(
            f"Invalid project name: '{name}'",
            hint="Name must start with a letter and contain only"
            " letters, digits, hyphens, and underscores.",
        )

    backend_raw = raw.get("backend", {}) or {}
    if not isinstance(backend_raw, dict):
        raise ConfigError("'backend' must be a mapping in tlumi.yaml.")
    _warn_unknown_keys(backend_raw, _KNOWN_BACKEND_KEYS, "'backend'")
    secrets_raw = raw.get("secrets", {}) or {}
    if not isinstance(secrets_raw, dict):
        raise ConfigError("'secrets' must be a mapping in tlumi.yaml.")
    _warn_unknown_keys(secrets_raw, _KNOWN_SECRETS_KEYS, "'secrets'")

    entry = _canonical_value(project.get("entry", DEFAULT_ENTRY))
    if not isinstance(entry, str):
        raise ConfigError(
            f"'project.entry' must be a string, got {type(entry).__name__}.",
            hint="Ensure 'project.entry' is a file path string in tlumi.yaml.",
        )

    # Validate the configured entry path doesn't escape the project directory.
    # Use a LEXICAL check (normpath, not resolve()) so a symlinked entry such as
    # the README's multi-environment layout (infra.py -> ../shared/infra.py) is
    # allowed: infra.py is user-trusted code that tlumi executes anyway, and the
    # configured string stays inside the project. The ".." and absolute-path
    # rejections in __post_init__ still block `entry: ../../etc/passwd`.
    entry_lexical = os.path.normpath(os.path.join(str(project_dir), entry))
    if os.path.commonpath([entry_lexical, str(project_dir)]) != str(project_dir):
        raise ConfigError(
            f"Entry path '{entry}' escapes the project directory.",
            hint="The 'project.entry' path must point to a file within the project.",
        )

    raw_variables = raw.get("variables", {}) or {}
    if not isinstance(raw_variables, dict):
        raise ConfigError("'variables' must be a mapping in tlumi.yaml.")
    variables: dict[str, str] = {}
    for k, v in raw_variables.items():
        if type(k) is not str:
            raise ConfigError(
                f"Variable key '{k}' in tlumi.yaml must be a quoted string.",
                hint=f'YAML coerced it from a keyword/number; quote it, e.g. "{k}": value.',
            )
        key_str = str(k)
        if ":" in key_str:
            raise ConfigError(
                f"Variable key '{key_str}' in tlumi.yaml contains ':'.",
                hint="Colons are reserved for Pulumi provider config namespaces.",
            )
        variables[key_str] = _coerce_variable_value(key_str, v, "tlumi.yaml")

    allow_unencrypted = _canonical_value(secrets_raw.get("allow_unencrypted", False))
    if not isinstance(allow_unencrypted, bool):
        raise ConfigError(
            "'secrets.allow_unencrypted' must be a boolean,"
            f" got {type(allow_unencrypted).__name__}.",
            hint="Use 'true' or 'false' (unquoted) in YAML, not a string like '\"false\"'.",
        )
    warn_unencrypted = _canonical_value(secrets_raw.get("warn_unencrypted", True))
    if not isinstance(warn_unencrypted, bool):
        raise ConfigError(
            f"'secrets.warn_unencrypted' must be a boolean, got {type(warn_unencrypted).__name__}.",
            hint="Use 'true' or 'false' (unquoted) in YAML, not a string like '\"false\"'.",
        )

    backend_url = _canonical_value(backend_raw.get("url"))
    if backend_url is not None and not isinstance(backend_url, str):
        raise ConfigError(
            f"'backend.url' must be a string, got {type(backend_url).__name__}.",
        )

    config = ProjectConfig(
        name=name,
        project_dir=project_dir,
        entry=entry,
        backend=BackendConfig(url=backend_url),
        secrets=SecretsConfig(
            warn_unencrypted=warn_unencrypted,
            allow_unencrypted=allow_unencrypted,
        ),
        variables=variables,
    )
    return config


_SENSITIVE_PATTERNS = {"secret", "password", "token", "key", "credential", "passphrase"}


def merge_variables(
    config: ProjectConfig,
    var: list[str] | None = None,
    var_file: list[str] | None = None,
    quiet: bool = False,
) -> ProjectConfig:
    """Return a new ProjectConfig with environment/file/CLI variables merged in.

    Priority: --var > --var-file > TLUMI_VAR_* > tlumi.yaml (already in
    ``config.variables``). Returns a new ProjectConfig instance rather than
    mutating in place so the frozen-dataclass invariant holds and every
    merge re-runs ``__post_init__`` validation on the result.
    """
    merged: dict[str, str] = dict(config.variables)

    # Layer 0: TLUMI_VAR_* environment variables
    for env_key, env_value in os.environ.items():
        if env_key.startswith("TLUMI_VAR_"):
            var_name = env_key[len("TLUMI_VAR_") :].lower()
            if var_name and ":" not in var_name:
                merged[var_name] = env_value

    # Layer 1: --var-file (each file overrides the previous)
    for path_str in var_file or []:
        path = Path(path_str)
        if not path.exists():
            raise ConfigError(f"Variable file not found: {path}")
        try:
            data = _load_yaml(path.read_text())
        except (yaml.YAMLError, OSError, UnicodeDecodeError, ValueError, RecursionError) as e:
            # Match load_config: PyYAML's timestamp constructor raises bare
            # ValueError and deeply nested documents raise RecursionError,
            # neither a yaml.YAMLError.
            raise ConfigError(f"Cannot read variable file {path}: {e}") from e
        if data is None:
            # An empty or comments-only var file means zero variables, mirroring
            # the `_load_yaml(...) or {}` normalization used for tlumi.yaml and
            # Terraform's acceptance of empty tfvars. A genuine non-dict document
            # (scalar, list) is still rejected below.
            data = {}
        if not isinstance(data, dict):
            raise ConfigError(f"Variable file must be a YAML mapping: {path}")
        for k, v in data.items():
            if type(k) is not str:
                raise ConfigError(
                    f"Variable key '{k}' in {path} must be a quoted string.",
                    hint=f'YAML coerced it from a keyword/number; quote it, e.g. "{k}": value.',
                )
            key_str = str(k)
            if ":" in key_str:
                raise ConfigError(
                    f"Variable key '{key_str}' in {path} contains ':'.",
                    hint="Colons are reserved for Pulumi provider config namespaces.",
                )
            merged[key_str] = _coerce_variable_value(key_str, v, str(path))

    # Layer 2: --var KEY=VALUE (highest priority)
    warned_sensitive = False
    for item in var or []:
        if "=" not in item:
            raise ConfigError(
                f"Invalid variable format: '{item}'",
                hint="Use --var KEY=VALUE format.",
            )
        key, _, value = item.partition("=")
        key = key.strip()
        if not key:
            raise ConfigError(
                f"Invalid variable format: '{item}'",
                hint="Use --var KEY=VALUE format.",
            )
        if ":" in key:
            raise ConfigError(
                f"Variable key '{key}' contains ':'.",
                hint="Colons are reserved for Pulumi provider config namespaces.",
            )
        merged[key] = value

        if not quiet and not warned_sensitive:
            key_lower = key.lower()
            if any(p in key_lower for p in _SENSITIVE_PATTERNS):
                from tlumi.display import print_warning

                print_warning(
                    f"--var '{key}' may contain sensitive data visible in shell history. "
                    "Consider using --var-file instead."
                )
                warned_sensitive = True

    return ProjectConfig(
        name=config.name,
        project_dir=config.project_dir,
        entry=config.entry,
        backend=config.backend,
        secrets=config.secrets,
        variables=merged,
    )


def find_project_dir() -> Path:
    """Return cwd if it contains tlumi.yaml, otherwise raise."""
    current = Path.cwd().resolve()
    if (current / CONFIG_FILE).exists():
        return current
    raise ProjectNotFoundError()
