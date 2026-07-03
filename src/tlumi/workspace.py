"""Pulumi Automation API wrapper for tlumi.

Manages the Pulumi workspace, stack, and CLI binary installation.
All Pulumi complexity is contained here; the rest of tlumi only
calls get_stack() and safe_export_stack() and uses the returned Stack object.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import site
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from urllib.parse import ParseResult

from pulumi import automation as auto

from tlumi._safeio import safe_chmod_dir, safe_mkdir, safe_write_text
from tlumi.config import DEFAULT_STACK, TLUMI_DIR, ProjectConfig
from tlumi.errors import ConfigError, WorkspaceError
from tlumi.redact import redact_text

_log = logging.getLogger(__name__)

_passphrase_warned = False
_gitignore_warned = False
_backend_warned = False


def _check_gitignore(config: ProjectConfig, *, quiet: bool = False) -> None:
    """Warn once if .tlumi/ is not in .gitignore."""
    global _gitignore_warned
    if _gitignore_warned or quiet:
        return
    gitignore = config.project_dir / ".gitignore"
    if gitignore.is_symlink():
        # A repo-committed symlink (e.g. .gitignore -> /dev/zero or /dev/stdin)
        # would make read_text() allocate without bound or block forever, so
        # skip the check rather than follow it -- consistent with the symlink
        # guards on every other repo-controlled read.
        _log.debug(".gitignore is a symlink, skipping check")
        return
    if not gitignore.exists():
        _log.debug("No .gitignore found, skipping check")
        return
    try:
        content = gitignore.read_text()
    except (OSError, UnicodeDecodeError):
        _log.debug("Cannot read .gitignore, skipping check", exc_info=True)
        return
    for line in content.splitlines():
        stripped = line.strip()
        if stripped in (TLUMI_DIR, f"{TLUMI_DIR}/", f"/{TLUMI_DIR}", f"/{TLUMI_DIR}/"):
            return
    _gitignore_warned = True
    _log.warning("'%s/' is not in .gitignore. Consider adding it.", TLUMI_DIR)


_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_key",
        "accesskey",
        "secret_key",
        "secretkey",
        "password",
        "passwd",
        "token",
        "sig",
        "signature",
        "sas",
        "se",
        "sp",
        "spr",
        "sv",
        "ss",
        "srt",
        "shared_access_key",
        "sharedaccesskey",
        "account_key",
        "accountkey",
        "auth",
        "authtoken",
        "authorization",
        "apikey",
        "api_key",
        "bearer",
        "secret",
        "aws_secret_access_key",
        "awssecretaccesskey",
        "aws_session_token",
        "awssessiontoken",
        "goog_token",
        "googtoken",
        "x_goog_token",
        "credential",
        "credentials",
        "private_key",
        "privatekey",
    }
)

# Substring patterns: any query key (lowercased) that contains one of these
# is treated as sensitive. Catches authToken, apiToken, oauth_token,
# refresh_token, accessToken, x-api-key, etc. without enumerating every
# vendor-specific variant.
_SENSITIVE_QUERY_SUBSTRINGS = (
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "apikey",
    "signature",
)


def _is_sensitive_query_key(key: str) -> bool:
    """Return True if the query parameter key looks credential-bearing."""
    k = key.lower()
    if k in _SENSITIVE_QUERY_KEYS:
        return True
    return any(s in k for s in _SENSITIVE_QUERY_SUBSTRINGS)


def _mask_query_string(query: str) -> tuple[str, bool]:
    """Return (masked_query, changed). Empty query returns ('', False)."""
    from urllib.parse import parse_qsl

    if not query:
        return "", False
    pairs = parse_qsl(query, keep_blank_values=True)
    masked_pairs: list[str] = []
    changed = False
    for key, value in pairs:
        if _is_sensitive_query_key(key):
            masked_pairs.append(f"{key}=***")
            changed = True
        else:
            masked_pairs.append(f"{key}={value}")
    return "&".join(masked_pairs), changed


def _fmt_host(hostname: str | None) -> str:
    """Re-wrap an IPv6 literal in brackets.

    ``urlparse(...).hostname`` strips the brackets from an IPv6 address, so
    rebuilding a netloc from it would yield an ambiguous/malformed host like
    ``2001:db8::1:9000`` (address vs. port). Restore the brackets when the host
    contains ``:``.
    """
    h = hostname or ""
    return f"[{h}]" if ":" in h else h


# Shown instead of a backend URL that cannot be parsed for masking: the URL
# may contain credentials, so the safe fallback is to hide it entirely.
_UNPARSEABLE_URL_PLACEHOLDER: Final = "<unparseable backend URL, hidden>"


def _port_suffix(parsed: ParseResult) -> str:
    """Return ``:<port>`` for rebuilding a masked netloc, or ``''``.

    ``parsed.port`` raises ValueError when the port text is non-numeric
    (a typo like ``host:port9000``). Omit the port from the masked display
    rather than propagate the error or echo raw netloc text back.
    """
    try:
        port = parsed.port
    except ValueError:
        return ""
    return f":{port}" if port is not None else ""


def _mask_backend_url(url: str) -> str:
    """Mask credentials in a backend URL for safe display. Never raises.

    Credentials can hide in four places: password in netloc (``user:pass@host``),
    bare-username-as-token in netloc (``token@host``, common for S3-compatible
    backends), sensitive query parameters (Azure SAS tokens, signed URLs,
    OAuth refresh tokens), and URL fragments that some platforms repurpose
    for tokens (``...#sig=...``). Mask all four.

    This function is the last line of defense before a backend URL reaches
    the console or logs, so any parse failure returns a fully redacted
    placeholder: never the raw URL (which may carry the very credentials
    the caller wants hidden) and never an exception (a typo in backend.url
    must not crash get_stack with a raw traceback).
    """
    try:
        return _mask_parsed_backend_url(url)
    except Exception:
        # Broad by design: malformed URLs (invalid IPv6 brackets, exotic
        # netlocs) raise from several places inside urllib; fail closed.
        _log.debug("Backend URL could not be parsed for masking; hiding it", exc_info=True)
        return _UNPARSEABLE_URL_PLACEHOLDER


def _mask_parsed_backend_url(url: str) -> str:
    """Masking body for _mask_backend_url; may raise on malformed URLs."""
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(url)

    changed = False
    masked_netloc = parsed.netloc

    if parsed.password:
        # user:pass@host: mask password, keep username visible
        masked_netloc = f"{parsed.username or ''}:***@{_fmt_host(parsed.hostname)}"
        masked_netloc += _port_suffix(parsed)
        changed = True
    elif parsed.username:
        # token@host: backend URLs almost never use bare usernames for
        # identification, so treat any lone user info as a credential.
        masked_netloc = f"***@{_fmt_host(parsed.hostname)}"
        masked_netloc += _port_suffix(parsed)
        changed = True

    masked_query, query_changed = _mask_query_string(parsed.query)
    if query_changed:
        changed = True

    # URL fragments are not standardized for queries, but some platforms
    # (notably OAuth implicit flow and certain signed-URL schemes) place
    # tokens after the '#'. Parse the fragment as a query when it contains
    # '=' so the same masking applies.
    masked_fragment = parsed.fragment
    if parsed.fragment and "=" in parsed.fragment:
        candidate, fragment_changed = _mask_query_string(parsed.fragment)
        if fragment_changed:
            masked_fragment = candidate
            changed = True

    if not changed:
        return url
    return urlunparse(
        parsed._replace(
            netloc=masked_netloc,
            # Only substitute the parse_qsl-rejoined query when a key was
            # actually masked. Otherwise keep the original verbatim: the rejoin
            # percent-decodes values (prefix=a%26b -> prefix=a&b), which would
            # mangle a benign query when only the netloc was masked.
            query=masked_query if query_changed else parsed.query,
            fragment=masked_fragment,
        )
    )


def _shared_cache_dir() -> Path | None:
    """Return the shared Pulumi CLI cache directory, or None if unavailable."""
    import importlib.metadata

    try:
        sdk_version = importlib.metadata.version("pulumi")
    except importlib.metadata.PackageNotFoundError:
        return None
    cache_dir = Path.home() / ".tlumi" / "cache" / "pulumi" / sdk_version
    return cache_dir


def _make_shared_cache_dir(dest_dir: Path) -> None:
    """Create the shared Pulumi CLI cache hierarchy with 0o700 on each level.

    ``mkdir(parents=True, mode=...)`` applies the mode only to the leaf, and
    ``exist_ok=True`` never re-chmods an existing dir, so ~/.tlumi and the
    intermediate dirs would otherwise keep the umask default (often
    world-traversable). Create the chain, then tighten each level under
    ~/.tlumi to 0o700, matching tlumi.uv._make_cache_dir (kept separate so
    the modules stay decoupled; a shared _safeio helper is a candidate
    follow-up). Chmod failures on a level we do not own are non-fatal.
    """
    dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    home = Path.home()
    try:
        rel = dest_dir.relative_to(home)
    except ValueError:
        return  # cache dir is not under $HOME (unusual); leaf mode already set
    current = home
    for part in rel.parts:
        current = current / part
        try:
            safe_chmod_dir(current, 0o700)
        except OSError:
            pass


def _ensure_pulumi_cli(config: ProjectConfig) -> auto.PulumiCommand:
    """Ensure the Pulumi CLI binary is installed, return a PulumiCommand.

    Tries shared cache (~/.tlumi/cache/pulumi/<version>/) first to avoid
    re-downloading the CLI binary for every project. Falls back to
    per-project .tlumi/cache/pulumi_home/ on failure.
    """
    # Try shared cache first -- catch broadly because any failure should
    # fall back to per-project install (PulumiCommand.install raises many
    # exception types: OSError, SubprocessError, InvalidVersionError, etc.)
    shared = _shared_cache_dir()
    if shared:
        try:
            _make_shared_cache_dir(shared)
            cmd = auto.PulumiCommand.install(root=str(shared))
            return cmd
        except (MemoryError, SystemExit):
            raise
        except Exception as e:
            # Surface this as a warning (not debug) so users learn the shared
            # cache is unhealthy. A silent fallback hides stale perms on
            # ~/.tlumi/ or corrupted cache and the eventual error is
            # disconnected from the root cause.
            _log.warning(
                "Shared Pulumi CLI cache install failed (%s); falling back to per-project cache.",
                e,
            )
            _log.debug("Shared cache install traceback", exc_info=True)

    # Per-project fallback
    install_dir = config.pulumi_home
    try:
        install_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as e:
        raise WorkspaceError(
            f"Cannot create Pulumi home directory: {e}",
            hint="Check disk space and directory permissions.",
        ) from e

    # Catch broadly because PulumiCommand.install() can raise many
    # exception types from the Pulumi SDK (OSError, SubprocessError,
    # InvalidVersionError, etc.) and there is no further fallback.
    try:
        cmd = auto.PulumiCommand.install(root=str(install_dir))
    except (MemoryError, SystemExit):
        raise
    except Exception as e:
        raise WorkspaceError(
            f"Failed to install Pulumi CLI: {e}",
            hint="Check your internet connection. Use --verbose for full details.",
        ) from e

    return cmd


def _load_inline_program(config: ProjectConfig) -> Callable[[], None]:
    """Load the user's infra.py as an inline Pulumi program function."""
    entry = config.entry_path
    if not entry.exists():
        raise WorkspaceError(
            f"Entry file not found: {entry}",
            hint=f"Create '{config.entry}' or update 'project.entry' in tlumi.yaml.",
        )

    # append() not insert(0): user files must not shadow stdlib modules
    # (e.g. a user's local "secrets.py" would otherwise hijack stdlib imports).
    project_str = str(config.project_dir)
    if project_str not in sys.path:
        sys.path.append(project_str)

    # If entry is in a subdirectory (e.g. src/infra.py), also add the parent
    # directory so sibling imports within the subdirectory work.
    entry_parent = str(entry.parent.resolve())
    if entry_parent != project_str and entry_parent not in sys.path:
        sys.path.append(entry_parent)

    # Add the venv site-packages so provider packages (pulumi-aws etc.) are importable.
    # We use site.addsitedir() instead of raw sys.path manipulation because it
    # processes .pth files and properly registers namespace packages.
    venv_site = _venv_site_packages(config)
    if venv_site and str(venv_site) not in sys.path:
        site.addsitedir(str(venv_site))

    module_name = f"_tlumi_entry.{entry.stem}"
    # User-code roots for module eviction. Symlinks make one logical location
    # reachable under several paths: a module imported through a symlinked
    # directory or file keeps the symlink in its __file__ (lexical form) while
    # Path.resolve() follows it to the target (resolved form). Cover the
    # project dir, the entry's parent dir (a symlinked subdir like
    # 'src -> ../shared'), and the resolved entry file's parent (a directly
    # symlinked entry file whose real siblings the program imports), each in
    # both forms, so shared helper code behind a symlink is still evicted
    # between program() calls instead of staying cached.
    user_roots = {
        config.project_dir,
        config.project_dir.resolve(),
        entry.parent,
        entry.parent.resolve(),
        entry.resolve().parent,
    }
    # .tlumi/ is excluded in both forms too: its venv site-packages hold
    # provider SDKs that must stay cached. The running interpreter's own
    # prefixes (sys.prefix / sys.base_prefix, both forms) are excluded for
    # the same reason: when the venv tlumi runs from sits under a user-code
    # root (e.g. a shared tooling venv inside a symlink-resolved
    # 'src -> ../shared' layout), evicting the pulumi SDK or tlumi itself
    # would make the user program's next 'import pulumi' re-execute the SDK
    # with fresh globals, losing runtime settings and sdk_compat patches.
    keep_roots = {config.tlumi_dir, config.tlumi_dir.resolve()}
    for prefix in (sys.prefix, sys.base_prefix):
        prefix_path = Path(prefix)
        keep_roots.update((prefix_path, prefix_path.resolve()))

    def _under_any(path: Path, roots: set[Path]) -> bool:
        return any(path.is_relative_to(root) for root in roots)

    def _evict_project_modules() -> None:
        # Evict every cached module whose source lives inside a user-code root
        # (excluding keep_roots: .tlumi/ and the interpreter's own prefixes,
        # whose site-packages hold SDKs that must stay cached). Popping only
        # the entry module is not enough: any project-local helper imported by
        # infra.py (the advertised "modules" pattern) would stay cached, so
        # its module-level resource registrations run only on the FIRST
        # program() call per process. In an interactive apply (preview then up
        # in one process) the up would then omit those resources, and Pulumi
        # would destroy them as if removed from the program. Fresh-loading the
        # whole user program every call matches the documented preview/up
        # semantics. Both the lexical and the resolved __file__ are checked so
        # a symlinked helper file is caught from either direction.
        for name, mod in list(sys.modules.items()):
            origin = getattr(mod, "__file__", None)
            if not origin:
                continue
            try:
                lexical = Path(origin)
                resolved = lexical.resolve()
            except (OSError, RuntimeError, ValueError, TypeError):
                # Pathological __file__ (non-str, embedded NUL, unresolvable,
                # or a symlink loop, which makes Python 3.10's resolve()
                # raise RuntimeError): skip this module instead of aborting
                # the whole run.
                continue
            for candidate in (lexical, resolved):
                if _under_any(candidate, user_roots) and not _under_any(candidate, keep_roots):
                    del sys.modules[name]
                    break

    def program():
        # Re-execute the user's whole program (entry + project-local imports)
        # each time so changes to infra.py and its helpers are picked up between
        # preview and apply calls within a single process.
        # Always use spec_from_file_location (not reload) because
        # the namespaced module name has no real parent package.
        _evict_project_modules()
        sys.modules.pop(module_name, None)
        spec = importlib.util.spec_from_file_location(module_name, str(entry))
        if spec is None or spec.loader is None:
            raise WorkspaceError(
                f"Cannot load entry file: {entry}",
                hint=f"Ensure '{config.entry}' is a valid Python file.",
            )
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise

    return program


def _venv_site_packages(config: ProjectConfig) -> Path | None:
    """Find the site-packages directory in the project venv."""
    venv = config.venv_dir
    if not venv.exists():
        _log.debug("Venv not found at %s", venv)
        return None
    lib_dir = venv / "lib"
    if not lib_dir.exists():
        _log.debug("Venv lib dir not found at %s", lib_dir)
        return None
    # Find python3.X directory
    try:
        children = list(lib_dir.iterdir())
    except OSError as e:
        _log.warning(
            "Cannot read venv lib directory %s: %s. Provider packages may not be available.",
            lib_dir,
            e,
        )
        return None
    for child in children:
        if child.name.startswith("python"):
            sp = child / "site-packages"
            if sp.exists():
                return sp
    _log.debug("No python directory found in %s", lib_dir)
    return None


# Sidecar under .tlumi/cache/ recording which bare config keys tlumi itself
# wrote to the stack. Stale-key cleanup consults it so tlumi never deletes
# config it does not own: "<project>:" prefix inference alone is unsafe when
# the project name collides with a provider namespace (a project named "aws"
# would otherwise see the user's real aws:region as its own stale variable).
_MANAGED_KEYS_FILENAME: Final = "managed_config_keys.json"


def _managed_keys_path(config: ProjectConfig) -> Path:
    return config.cache_dir / _MANAGED_KEYS_FILENAME


def _read_managed_keys(config: ProjectConfig) -> set[str] | None:
    """Read the sidecar of config keys tlumi wrote on a previous run.

    Returns None when the sidecar is missing, unreadable, or malformed;
    callers must then skip stale-key removal entirely. Failing to clean a
    stale variable is safe but not self-healing: a key whose variable is
    removed from tlumi.yaml while no sidecar exists is never recorded again,
    so it stays in Pulumi config until removed manually. Deleting config
    tlumi does not own is worse, so no sidecar still means no removal.
    """
    path = _managed_keys_path(config)
    if path.is_symlink():
        _log.warning("%s is a symlink; ignoring it.", path)
        return None
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError: byte-level corruption is malformed content too;
        # skip cleanup instead of crashing every runtime command.
        _log.debug("Cannot read managed-config sidecar %s", path, exc_info=True)
        return None
    try:
        keys = json.loads(raw)["keys"]
    except (ValueError, TypeError, KeyError, RecursionError):
        # RecursionError: a pathologically nested JSON file (e.g. an
        # attacker-shipped .tlumi/) is treated like any other corrupt sidecar.
        _log.debug("Malformed managed-config sidecar %s", path, exc_info=True)
        return None
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        _log.debug("Malformed managed-config sidecar %s (bad 'keys' shape)", path)
        return None
    return set(keys)


def _write_managed_keys(config: ProjectConfig, keys: set[str]) -> None:
    """Record the config keys tlumi wrote, for the next run's cleanup.

    Best-effort: a write failure means stale-key cleanup stays skipped until
    a later run rewrites the sidecar (the safe direction), so warn instead
    of failing the command. It is not a one-run skip: a variable removed
    from tlumi.yaml while no sidecar exists is never recorded again, so its
    config key is never cleaned automatically. The warning says so.
    """
    path = _managed_keys_path(config)
    payload = json.dumps({"keys": sorted(keys)}) + "\n"
    try:
        safe_write_text(path, payload, mode=0o600)
    except OSError:
        _log.warning(
            "Could not record managed config keys at %s;"
            " stale variable cleanup is skipped until a later run rewrites"
            " this file. A variable removed from tlumi.yaml in the meantime"
            " is never cleaned up automatically.",
            path,
        )
        _log.debug("Managed-config sidecar write failure", exc_info=True)


def safe_export_stack(stack: auto.Stack) -> auto.Deployment:
    """Export stack state, wrapping CommandError in WorkspaceError.

    The redacted Pulumi stderr is included in the message (matching the
    CommandError-interpolation pattern used elsewhere) so the real cause is
    visible on the default masked paths: e.g. a wrong TLUMI_SECRETS_PASSPHRASE
    surfaces as "incorrect passphrase" with a passphrase hint instead of a
    misleading state-unlock hint.
    """
    from pulumi.automation import CommandError

    try:
        return stack.export_stack()
    except CommandError as e:
        detail = redact_text(str(e))
        if "incorrect passphrase" in detail.lower():
            hint = "Check TLUMI_SECRETS_PASSPHRASE is set correctly."
        else:
            hint = "Run 'tlumi state unlock' if the state is locked."
        raise WorkspaceError(f"Failed to read state: {detail}", hint=hint) from e
    except RecursionError as e:
        # The SDK's export_stack json.loads-parses the exported state. Go's
        # encoding/json accepts deeper nesting than CPython's json (~1000 on
        # 3.10-3.12), so an attacker-shipped .tlumi/state nested past that limit
        # raises RecursionError here instead of a clean error.
        raise WorkspaceError(
            "Failed to read state: state file nesting is too deep.",
            hint="The state file may be corrupt or malicious.",
        ) from e


def export_stack_no_secrets(stack: auto.Stack) -> auto.Deployment:
    """Export stack state WITHOUT decrypting secrets (ciphertext preserved).

    ``Stack.export_stack()`` and the SDK's ``LocalWorkspace.export_stack`` both
    hardcode ``pulumi stack export --show-secrets``, which decrypts every secret
    into its plaintext wrapper. That is correct for ``state pull`` (an explicit,
    user-controlled restore round-trip to stdout), but wrong for the on-disk
    backups that ``state rm``/``mv``/``push`` write under ``.tlumi/backups/``:
    those must not persist decrypted secrets even when TLUMI_SECRETS_PASSPHRASE
    is set. Running the plain ``stack export`` keeps every secret as ciphertext,
    which restores identically because the encryption salt travels inside
    ``deployment.secrets_providers`` and ``state push`` requires the same
    passphrase.

    Error handling mirrors ``safe_export_stack``: CommandError is redacted and
    wrapped in WorkspaceError, and deeply nested state raises a clean error.
    """
    from pulumi.automation import CommandError

    try:
        result = stack._run_pulumi_cmd_sync(["stack", "export"])
        state_json = json.loads(result.stdout)
        return auto.Deployment(
            version=state_json.get("version"),
            deployment=state_json.get("deployment"),
        )
    except CommandError as e:
        detail = redact_text(str(e))
        if "incorrect passphrase" in detail.lower():
            hint = "Check TLUMI_SECRETS_PASSPHRASE is set correctly."
        else:
            hint = "Run 'tlumi state unlock' if the state is locked."
        raise WorkspaceError(f"Failed to read state: {detail}", hint=hint) from e
    except RecursionError as e:
        raise WorkspaceError(
            "Failed to read state: state file nesting is too deep.",
            hint="The state file may be corrupt or malicious.",
        ) from e


def get_stack(
    config: ProjectConfig,
    install_cli: bool = True,
    quiet: bool = False,
    runtime: bool = True,
    reconcile_config: bool = True,
) -> auto.Stack:
    """Create or select the default Pulumi stack for this project.

    Args:
        runtime: When True (default), load the user's infra.py as the stack's
            inline program and reconcile stack config from tlumi.yaml. When
            False (state-only mode), use a no-op program and skip config
            reconciliation -- this lets recovery commands like state
            list/show/pull/rm/mv/push and state unlock run even when infra.py
            is broken, the venv is missing, or stack config is unreadable.
            The program is never invoked during state read/import operations,
            only during preview()/up()/destroy()/refresh().
        reconcile_config: When True (default), reconcile stack config from the
            merged variables (remove stale sidecar-recorded keys, set current
            ones). Pass False for ``apply --plan``: the saved plan captures the
            exact config it was generated against and ``apply --plan`` forbids
            re-supplying --var/--var-file/TLUMI_VAR_*, so reconciliation would
            wrongly remove plan-time variables (present in the sidecar but
            absent from the now-narrower merged variables) before up(plan=...).
            Pulumi saved plans do not re-inject config into the in-process
            program, so that removal would break config.require() at apply time.

    Handles: Pulumi CLI install, backend configuration, pulumi_home isolation,
    and (in runtime mode) loading the user's infra.py.
    """
    # Guard against .tlumi being a symlink (malicious repository)
    if config.tlumi_dir.is_symlink():
        raise WorkspaceError(
            f".tlumi is a symlink to {os.readlink(config.tlumi_dir)}",
            hint="Remove the symlink. A malicious repository may have created it.",
        )

    # The venv is only needed to import providers when infra.py runs. State
    # recovery commands must not depend on a healthy venv.
    if runtime and not config.venv_dir.exists():
        raise WorkspaceError(
            "Project not initialized.",
            hint="Run 'tlumi init' to set up the project.",
        )

    # Check subdirectories for symlinks before creating them
    for subdir in (config.state_dir, config.cache_dir, config.pulumi_home):
        if subdir.is_symlink():
            raise WorkspaceError(
                f"{subdir.name} is a symlink to {os.readlink(subdir)}",
                hint="Remove the symlink. A malicious repository may have created it.",
            )

    # Ensure directories exist. tlumi_dir must exist before its children;
    # safe_mkdir only handles a single level, so create parents explicitly.
    try:
        safe_mkdir(config.tlumi_dir, mode=0o700)
        safe_mkdir(config.cache_dir, mode=0o700)
        safe_mkdir(config.state_dir, mode=0o700)
        safe_mkdir(config.pulumi_home, mode=0o700)
    except OSError as e:
        raise WorkspaceError(
            f"Cannot create workspace directories: {e}",
            hint="Check disk space and directory permissions.",
        ) from e

    # Prevent __pycache__ in project directory from inline program imports
    sys.dont_write_bytecode = True

    pulumi_cmd = _ensure_pulumi_cli(config) if install_cli else None

    if runtime:
        program: Callable[[], None] = _load_inline_program(config)
    else:
        # No-op: state read/import operations never invoke the program. This
        # is the seam that lets recovery work without loading infra.py.
        def program() -> None:
            return None

    backend_url = config.backend_url()

    env_vars = {
        "PULUMI_BACKEND_URL": backend_url,
        # Empty token is Pulumi's documented opt-out for the Cloud backend;
        # without this Pulumi prompts for login on first run.
        "PULUMI_ACCESS_TOKEN": "",  # nosec B105
        "PULUMI_SKIP_UPDATE_CHECK": "true",
        "PULUMI_HOME": str(config.pulumi_home),
        "PYTHONDONTWRITEBYTECODE": "1",
        # Scrub tlumi-only env vars before handing off to provider plugins.
        # Pulumi's LocalWorkspace uses additional_env semantics (parent env
        # inherits), and providers can spawn third-party processes. The
        # passphrase is only needed translated as PULUMI_CONFIG_PASSPHRASE
        # below, and TLUMI_VAR_* values are already promoted to Pulumi config
        # via set_config(), so neither needs to remain in the subprocess env.
        "TLUMI_SECRETS_PASSPHRASE": "",  # nosec B105
    }
    for var_name in os.environ:
        if var_name.startswith("TLUMI_VAR_"):
            env_vars[var_name] = ""

    # Pass secrets passphrase if set via environment
    global _passphrase_warned
    passphrase = os.environ.get("TLUMI_SECRETS_PASSPHRASE", "")
    if passphrase:
        env_vars["PULUMI_CONFIG_PASSPHRASE"] = passphrase
    elif config.secrets.allow_unencrypted:
        env_vars["PULUMI_CONFIG_PASSPHRASE"] = ""  # nosec B105 - explicit empty opt-in
        if not _passphrase_warned and not quiet and config.secrets.warn_unencrypted:
            _passphrase_warned = True
            _log.info("Secrets are not encrypted (no TLUMI_SECRETS_PASSPHRASE set).")
    else:
        raise ConfigError(
            "No TLUMI_SECRETS_PASSPHRASE set and unencrypted secrets not allowed.",
            hint="Set TLUMI_SECRETS_PASSPHRASE env var, or add"
            " 'secrets.allow_unencrypted: true' to tlumi.yaml.",
        )

    _check_gitignore(config, quiet=quiet)

    global _backend_warned
    if not _backend_warned and not quiet and backend_url and not backend_url.startswith("file://"):
        _backend_warned = True
        display_url = _mask_backend_url(backend_url)
        _log.info("Backend: %s", display_url)

    # Use .tlumi/ as work_dir so Pulumi writes its Pulumi.yaml and
    # Pulumi.<stack>.yaml files there instead of polluting the project root.
    opts = auto.LocalWorkspaceOptions(
        work_dir=str(config.tlumi_dir),
        pulumi_home=str(config.pulumi_home),
        project_settings=auto.ProjectSettings(
            name=config.name,
            runtime="python",
            backend=auto.ProjectBackend(url=backend_url),
        ),
        env_vars=env_vars,
    )

    if pulumi_cmd:
        opts.pulumi_command = pulumi_cmd

    try:
        stack = auto.create_or_select_stack(
            stack_name=DEFAULT_STACK,
            project_name=config.name,
            program=program,
            opts=opts,
        )
    except auto.errors.ConcurrentUpdateError as e:
        raise WorkspaceError(
            "Another tlumi operation is in progress.",
            hint="Wait for it to finish, or run 'tlumi state unlock' to release the lock.",
        ) from e
    except auto.errors.CommandError as e:
        # Pulumi stderr can echo the backend URL with embedded credentials
        # (AWS keys, Azure SAS tokens); redact before it reaches the console
        # or the --json error envelope.
        raise WorkspaceError(f"Failed to initialize workspace: {redact_text(str(e))}") from e

    # State-only commands skip config reconciliation: they don't run infra.py
    # so the project's variables don't matter, and reaching into stack config
    # would couple recovery to the very thing that may be broken.
    if not runtime:
        return stack

    # apply --plan: preserve the plan-time stack config. The saved plan carries
    # the config it was generated against and up(plan=...) does not re-inject it
    # into the in-process program, so reconciling here (which would remove
    # plan-time variables absent from the now-narrower merged variables) would
    # break config.require() at apply time. See the reconcile_config docstring.
    if not reconcile_config:
        return stack

    # Remove stale config keys before setting current variables. Ownership
    # comes from the managed-keys sidecar (the keys tlumi itself wrote on a
    # previous run), never from the "<project>:" prefix alone: for a project
    # named after a provider (e.g. "aws"), a real provider key like
    # aws:region also yields a bare single-segment key, and prefix inference
    # would delete the user's provider config. No sidecar means no removal.
    # This remove-then-set loop is not atomic, but atomicity is unnecessary:
    # Pulumi's config API has no batch operation, and if set_config fails
    # mid-loop, the next run will reconcile by repeating this cleanup.
    managed_keys = _read_managed_keys(config)
    if managed_keys is None:
        _log.debug(
            "No managed-config sidecar; skipping stale config key cleanup"
            " (expected on the first run after an upgrade or 'tlumi clean')"
        )
    else:
        try:
            existing = stack.get_all_config()
        except auto.errors.CommandError:
            _log.debug(
                "Config cleanup skipped (expected on first init;"
                " also harmless if config is temporarily unavailable)",
                exc_info=True,
            )
            existing = {}

        project_prefix = f"{config.name}:"
        for key in existing:
            if not key.startswith(project_prefix):
                continue
            bare_key = key[len(project_prefix) :]
            if ":" in bare_key:
                continue  # nested namespace (e.g. aws:s3:opt when project is "aws")
            if bare_key not in managed_keys:
                continue  # not written by tlumi: leave it alone
            if bare_key in config.variables:
                continue
            try:
                stack.remove_config(bare_key)
            except auto.errors.CommandError as e:
                # Symmetric with set_config below: a soft warning here leaves
                # the stale key silently active across runs (the user would
                # see a "removed" variable still applying). Raise so the user
                # can investigate (typically a locked state).
                raise WorkspaceError(
                    f"Failed to remove stale config key '{key}': {redact_text(str(e))}",
                    hint="Check that the Pulumi stack is not locked.",
                ) from e

    for key, value in config.variables.items():
        try:
            stack.set_config(key, auto.ConfigValue(value=str(value)))
        except auto.errors.CommandError as e:
            raise WorkspaceError(
                f"Failed to set config variable '{key}': {redact_text(str(e))}",
                hint="Check that the Pulumi stack is not locked.",
            ) from e

    _write_managed_keys(config, set(config.variables))

    return stack
