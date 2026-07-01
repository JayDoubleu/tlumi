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
# Plain decimal ints only: these are the sole int forms whose text survives a
# parse-then-stringify round trip. Everything else PyYAML's YAML 1.1 int
# resolver accepts (leading-zero octal, 0x/0b, underscore separators,
# sexagesimal) is remapped to _NON_DECIMAL_INT_TAG below.
_DECIMAL_INT_RE = re.compile(r"^[-+]?(?:0|[1-9][0-9]*)\Z")


class _NonDecimalIntText(str):
    """Raw text of a YAML 1.1 non-decimal int form (octal/hex/sexagesimal/...).

    PyYAML silently resolves ``0777`` to 511 and ``1:30`` to 90, the same
    lossy-rewrite class that gets floats rejected in ``_coerce_variable_value``.
    The strict loader preserves the original text in this marker type so the
    coercion step can reject it with a hint that quotes the value the user
    actually wrote. Subclassing ``str`` keeps non-variable config fields
    (which validate against ``str``) behaving sensibly for these scalars.
    """

    __slots__ = ()


class _StrictIntLoader(yaml.SafeLoader):
    """SafeLoader that refuses to lossily rewrite non-decimal int scalars.

    Identical to ``yaml.SafeLoader`` except that only plain decimal scalars
    resolve to ints; the remaining YAML 1.1 int forms resolve to
    ``_NonDecimalIntText`` carrying the raw text. Resolver order for all
    other tags is preserved exactly.
    """


def _construct_non_decimal_int(
    loader: yaml.SafeLoader, node: yaml.ScalarNode
) -> _NonDecimalIntText:
    return _NonDecimalIntText(node.value)


_StrictIntLoader.add_constructor(_NON_DECIMAL_INT_TAG, _construct_non_decimal_int)


def _build_strict_int_resolvers() -> dict[str | None, list[tuple[str, re.Pattern[str]]]]:
    """Copy SafeLoader's implicit resolvers, splitting the int resolver in two.

    The stock int entry is replaced in place by a strict decimal resolver
    (still tagged as int) followed by the original YAML 1.1 regex retagged as
    ``_NON_DECIMAL_INT_TAG``, so anything the stock loader would have made an
    int is guaranteed to hit one of the two.
    """
    resolvers: dict[str | None, list[tuple[str, re.Pattern[str]]]] = {}
    for first_char, pairs in yaml.SafeLoader.yaml_implicit_resolvers.items():
        rebuilt = []
        for tag, regexp in pairs:
            if tag == _YAML_INT_TAG:
                rebuilt.append((_YAML_INT_TAG, _DECIMAL_INT_RE))
                rebuilt.append((_NON_DECIMAL_INT_TAG, regexp))
            else:
                rebuilt.append((tag, regexp))
        resolvers[first_char] = rebuilt
    return resolvers


_StrictIntLoader.yaml_implicit_resolvers = _build_strict_int_resolvers()


def _load_yaml(text: str) -> object:
    """Parse YAML with the strict int loader (used for tlumi.yaml and var files)."""
    # _StrictIntLoader subclasses yaml.SafeLoader, so this is exactly as safe
    # as yaml.safe_load(); bandit only pattern-matches the yaml.load call.
    return yaml.load(text, Loader=_StrictIntLoader)  # nosec B506


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
    values. Non-decimal int forms (``0777`` -> 511, ``1:30`` -> 90, ``0x1A`` ->
    26) are the same lossy-rewrite class; the strict loader preserves their raw
    text as ``_NonDecimalIntText`` and they are rejected here with a quote hint.
    Lists/dicts and bare/null values are rejected as before.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        raise ConfigError(
            f"Variable '{key}' in {source} must be a scalar value, not {type(value).__name__}.",
        )
    if value is None:
        raise ConfigError(
            f"Variable '{key}' in {source} has no value.",
            hint='Remove the key or give it an explicit value (use "" for empty).',
        )
    if isinstance(value, float):
        raise ConfigError(
            f"Variable '{key}' in {source} is an unquoted decimal ({value!r}); "
            "YAML silently rewrites values like 1.10 to 1.1.",
            hint=f'Quote it to preserve the exact text, e.g. {key}: "{value}".',
        )
    if isinstance(value, _NonDecimalIntText):
        raise ConfigError(
            f"Variable '{key}' in {source} is an unquoted non-decimal number ({value}); "
            "YAML silently rewrites values like 0777 to 511 and 1:30 to 90.",
            hint=f'Quote it to preserve the exact text, e.g. {key}: "{value}".',
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
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
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

    name = project.get("name")
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

    entry = project.get("entry", DEFAULT_ENTRY)
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
        key_str = str(k)
        if ":" in key_str:
            raise ConfigError(
                f"Variable key '{key_str}' in tlumi.yaml contains ':'.",
                hint="Colons are reserved for Pulumi provider config namespaces.",
            )
        variables[key_str] = _coerce_variable_value(key_str, v, "tlumi.yaml")

    allow_unencrypted = secrets_raw.get("allow_unencrypted", False)
    if not isinstance(allow_unencrypted, bool):
        raise ConfigError(
            "'secrets.allow_unencrypted' must be a boolean,"
            f" got {type(allow_unencrypted).__name__}.",
            hint="Use 'true' or 'false' (unquoted) in YAML, not a string like '\"false\"'.",
        )
    warn_unencrypted = secrets_raw.get("warn_unencrypted", True)
    if not isinstance(warn_unencrypted, bool):
        raise ConfigError(
            f"'secrets.warn_unencrypted' must be a boolean, got {type(warn_unencrypted).__name__}.",
            hint="Use 'true' or 'false' (unquoted) in YAML, not a string like '\"false\"'.",
        )

    backend_url = backend_raw.get("url")
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
        except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
            raise ConfigError(f"Cannot read variable file {path}: {e}") from e
        if not isinstance(data, dict):
            raise ConfigError(f"Variable file must be a YAML mapping: {path}")
        for k, v in data.items():
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
