"""uv binary management for tlumi.

Handles discovery, download, and invocation of the uv package manager.
The discovery order mirrors the Pulumi CLI strategy in workspace.py,
though the implementation differs: PATH first, then shared cache, then
download from GitHub releases.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import platform
import shutil
import subprocess  # nosec B404
import sysconfig
import tarfile
from collections.abc import Callable
from pathlib import Path

from tlumi.errors import WorkspaceError

# Refresh this pin each release: check https://github.com/astral-sh/uv/releases
# and confirm the uv-<target>.tar.gz / .sha256 asset naming still matches
# _download_uv() and _verify_checksum().
_UV_VERSION = "0.11.26"

_PLATFORM_MAP: dict[tuple[str, str], str] = {
    ("Linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("Linux", "aarch64"): "aarch64-unknown-linux-gnu",
    ("Darwin", "x86_64"): "x86_64-apple-darwin",
    ("Darwin", "arm64"): "aarch64-apple-darwin",
}


def _is_musl_libc() -> bool:
    """Best-effort detection of a musl-based Linux system (Alpine et al.).

    uv's ``*-linux-gnu`` release binaries are dynamically linked against the
    glibc loader (/lib64/ld-linux-*.so), which does not exist on musl
    distros: exec then fails with a misleading ENOENT naming the uv binary
    itself. The ``*-linux-musl`` builds are statically linked and run on
    both libcs, so when detection is ambiguous musl is the safe choice.

    Heuristics: HOST_GNU_TYPE contains "musl" for Pythons built on musl
    (Alpine packages, musllinux wheels); platform.libc_ver() identifies
    glibc by inspecting the running executable and returns ("", "") on musl.
    """
    if "musl" in (sysconfig.get_config_var("HOST_GNU_TYPE") or ""):
        return True
    return platform.libc_ver()[0] != "glibc"


def _platform_target() -> str:
    """Return the uv release target triple for the current platform."""
    key = (platform.system(), platform.machine())
    target = _PLATFORM_MAP.get(key)
    if not target:
        raise WorkspaceError(
            f"Unsupported platform: {key[0]} {key[1]}",
            hint="Install uv manually: https://docs.astral.sh/uv/getting-started/installation/",
        )
    if key[0] == "Linux" and target.endswith("-gnu") and _is_musl_libc():
        target = target.removesuffix("-gnu") + "-musl"
    return target


def _shared_uv_cache_dir() -> Path:
    """Return the shared uv cache directory: ~/.tlumi/cache/uv/<version>/."""
    return Path.home() / ".tlumi" / "cache" / "uv" / _UV_VERSION


def _find_uv() -> Path | None:
    """Check if uv is available on PATH."""
    found = shutil.which("uv")
    return Path(found) if found else None


def _verify_checksum(url: str, archive_path: Path, filename: str) -> None:
    """Verify SHA-256 checksum of a downloaded archive.

    Downloads the .sha256 checksum file from the same release URL and compares
    it against the computed hash. On mismatch or checksum fetch failure, deletes
    the archive and raises WorkspaceError.
    """
    import urllib.request

    checksum_url = f"{url}.sha256"
    try:
        with urllib.request.urlopen(checksum_url, timeout=10) as response:  # nosec B310
            checksum_data = response.read().decode().strip()
            expected = checksum_data.split()[0]
    except (OSError, http.client.HTTPException) as e:
        archive_path.unlink(missing_ok=True)
        raise WorkspaceError(
            f"Failed to download checksum file for {filename}: {e}",
            hint="The archive was downloaded but its checksum could not be fetched. "
            "Try again or install uv manually.",
        ) from e
    except (UnicodeDecodeError, IndexError) as e:
        archive_path.unlink(missing_ok=True)
        raise WorkspaceError(
            f"Failed to parse checksum file for {filename}: {e}",
            hint="The checksum file may be corrupted. Try again or install uv manually.",
        ) from e

    try:
        actual = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    except OSError as e:
        archive_path.unlink(missing_ok=True)
        raise WorkspaceError(
            f"Cannot read downloaded archive for checksum verification: {e}",
            hint="Try again or install uv manually.",
        ) from e
    if actual != expected:
        archive_path.unlink(missing_ok=True)
        raise WorkspaceError(
            f"SHA-256 mismatch for {filename} (expected {expected[:16]}..., got {actual[:16]}...)",
            hint="The downloaded binary may be corrupted or tampered with."
            " Try again or install uv manually.",
        )


def _make_cache_dir(dest_dir: Path) -> None:
    """Create the shared uv cache hierarchy with 0o700 on each level.

    ``mkdir(parents=True, mode=...)`` applies the mode only to the leaf, and
    ``exist_ok=True`` never re-chmods an existing dir, so ~/.tlumi and its
    intermediate dirs would otherwise keep the umask default (often
    world-traversable). Create the chain, then tighten each level under ~/.tlumi
    to 0o700. Chmod failures on a level we do not own are non-fatal.
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
            current.chmod(0o700)
        except OSError:
            pass


def _download_uv(dest_dir: Path) -> Path:
    """Download uv binary from GitHub releases into dest_dir.

    Returns the path to the extracted uv binary.
    """
    import urllib.request

    target = _platform_target()
    filename = f"uv-{target}.tar.gz"
    url = f"https://github.com/astral-sh/uv/releases/download/{_UV_VERSION}/{filename}"

    try:
        _make_cache_dir(dest_dir)
    except OSError as e:
        raise WorkspaceError(
            f"Cannot create uv cache directory: {e}",
            hint="Check disk space and directory permissions.",
        ) from e
    # Pid-unique archive path: two concurrent first-run downloads must not open
    # the same file with "wb" and truncate each other's bytes (which would trip
    # a false "may be corrupted or tampered with" checksum mismatch). Mirrors the
    # pid-unique tmp_dest used for the extracted binary below (F13).
    archive_path = dest_dir / f"{filename}.{os.getpid()}.tmp"

    try:
        with urllib.request.urlopen(url, timeout=60) as response:  # nosec B310
            with open(archive_path, "wb") as f:
                shutil.copyfileobj(response, f)
    except (OSError, http.client.HTTPException) as e:
        archive_path.unlink(missing_ok=True)
        raise WorkspaceError(
            f"Failed to download uv: {e}",
            hint="Check your internet connection, or install uv manually: https://docs.astral.sh/uv/getting-started/installation/",
        ) from e

    # Verify SHA-256 checksum
    _verify_checksum(url, archive_path, filename)

    uv_dest = dest_dir / "uv"
    # Extract to a unique temp path, chmod it, then atomically replace the final
    # path. A concurrent tlumi process must never observe a half-written or
    # not-yet-executable binary at uv_dest (F13); a failed extraction or chmod
    # must not poison the cache with a partial binary (F14) -- only a complete,
    # executable binary is ever moved into place.
    tmp_dest = dest_dir / f"uv.{os.getpid()}.tmp"
    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            for member in tar.getmembers():
                if member.name.endswith("/uv") and member.isfile():
                    # Manual extract: tar.extract() on Python 3.10/3.11 defaults to
                    # no filter and follows symlinks/absolute paths inside the archive.
                    # Even on 3.12+ with filter="data", manual write keeps a single
                    # safe code path. The checksum verification above is the primary
                    # control; this is defense-in-depth.
                    extracted = tar.extractfile(member)
                    if extracted is None:
                        raise WorkspaceError("uv binary in archive is not a regular file.")
                    with extracted, open(tmp_dest, "wb") as out:
                        shutil.copyfileobj(extracted, out)
                    break
            else:
                raise WorkspaceError("uv binary not found in downloaded archive.")
        os.chmod(tmp_dest, 0o700)
        os.replace(tmp_dest, uv_dest)
    except tarfile.TarError as e:
        raise WorkspaceError(f"Failed to extract uv archive: {e}") from e
    except OSError as e:
        raise WorkspaceError(
            f"Failed to install uv binary: {e}",
            hint="Check disk space and file permissions, or install uv manually.",
        ) from e
    finally:
        archive_path.unlink(missing_ok=True)
        # Remove the temp binary unless it was atomically moved into place
        # (extraction error, chmod/replace failure, or interrupt).
        tmp_dest.unlink(missing_ok=True)

    return uv_dest


def ensure_uv() -> Path:
    """Find or auto-install the uv binary.

    Discovery order:
    1. PATH (shutil.which)
    2. Shared cache (~/.tlumi/cache/uv/<version>/uv)
    3. Download from GitHub releases into shared cache
    """
    # 1. Check PATH
    found = _find_uv()
    if found:
        return found

    # 2. Check shared cache. Require a non-empty, executable file so a partial
    # or non-executable binary left by an interrupted older run is not returned
    # forever (it would never self-heal: `tlumi clean` only touches the
    # project-local .tlumi/, not ~/.tlumi/cache/).
    cache_dir = _shared_uv_cache_dir()
    cached = cache_dir / "uv"
    try:
        if cached.is_file() and cached.stat().st_size > 0 and os.access(cached, os.X_OK):
            return cached
    except OSError:
        pass  # fall through to re-download

    # 3. Download
    return _download_uv(cache_dir)


def _uv_exec_error(uv: Path, e: OSError) -> WorkspaceError:
    """Build a WorkspaceError for a failure to exec the uv binary.

    A FileNotFoundError on a binary that exists and is executable means the
    dynamic loader is missing -- almost always a glibc-linked uv binary on a
    musl system (Alpine). Point the user at the real cause and a fix rather than
    the misleading "binary removed or not executable".
    """
    hint = "The uv binary may have been removed or is not executable."
    if isinstance(e, FileNotFoundError):
        try:
            if uv.is_file() and os.access(uv, os.X_OK):
                hint = (
                    "The uv binary exists but its dynamic loader is missing "
                    "(likely a glibc/musl mismatch). Delete ~/.tlumi/cache/uv and "
                    "retry, or install uv from your system package manager."
                )
        except OSError:
            pass
    return WorkspaceError(f"Failed to run uv: {e}", hint=hint)


def create_venv(uv: Path, venv_path: Path) -> None:
    """Create a Python virtual environment using uv (no pip seeded)."""
    try:
        subprocess.run(  # nosec B603
            [str(uv), "venv", str(venv_path)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        raise WorkspaceError(
            f"Failed to create virtual environment: {e.stderr.strip()}",
            hint="Ensure Python is installed on your system.",
        ) from e
    except OSError as e:
        raise _uv_exec_error(uv, e) from e


def _stat_device(path: Path) -> int | None:
    """Device id (st_dev) of path or its nearest existing ancestor.

    Returns None if nothing along the chain can be stat-ed. The venv target may
    not exist yet on a first install, so we walk up to the first real directory,
    which is on the same filesystem the venv will occupy. This is a best-effort
    hint, so an unreadable directory (OSError) or an invalid path such as an
    embedded NUL (ValueError) yields None rather than propagating.
    """
    for candidate in (path, *path.parents):
        try:
            return candidate.stat().st_dev
        except (OSError, ValueError):
            continue
    return None


def _uv_cache_device(uv: Path) -> int | None:
    """Device id of uv's package cache directory, or None if undeterminable.

    ``uv cache dir`` prints the cache path (honouring UV_CACHE_DIR and the
    inherited environment) even before it exists, so we resolve the device from
    the nearest existing ancestor.
    """
    try:
        result = subprocess.run(  # nosec B603
            [str(uv), "cache", "dir"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    cache_path = result.stdout.strip()
    if not cache_path:
        return None
    return _stat_device(Path(cache_path))


def _link_mode_args(uv: Path, venv_path: Path) -> list[str]:
    """Return ``["--link-mode=copy"]`` when the venv and uv's package cache are on
    different filesystems, else ``[]``.

    uv links cached wheels into the venv by default and prints a
    "Failed to hardlink files; falling back to full copy" warning when the venv
    lives on a different filesystem from ~/.cache/uv (its default link modes,
    hardlink and clone/reflink, both require a single filesystem; common: a project on a data
    mount, /tmp, or a container volume separate from $HOME). Forcing copy mode in
    that case suppresses the noise at no real cost, because uv falls back to a
    full copy there regardless. On the same filesystem we leave uv's faster
    clone/hardlink default untouched. If either device is undeterminable we make
    no change and let uv behave as it would by default.
    """
    cache_dev = _uv_cache_device(uv)
    venv_dev = _stat_device(venv_path)
    if cache_dev is None or venv_dev is None or cache_dev == venv_dev:
        return []
    return ["--link-mode=copy"]


def run_install(
    uv: Path,
    venv_path: Path,
    args: list[str],
    add_line: Callable[[str], None] | None = None,
) -> None:
    """Install packages using uv pip install.

    When add_line is None, runs quietly with captured output.
    When add_line is a callable, streams output line-by-line for live display.
    """
    cmd = [
        str(uv),
        "pip",
        "install",
        *_link_mode_args(uv, venv_path),
        "--python",
        str(venv_path / "bin" / "python"),
        *args,
    ]

    if add_line is None:
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)  # nosec B603
        except subprocess.CalledProcessError as e:
            raise WorkspaceError(
                f"Failed to install dependencies: {e.stderr.strip()}",
            ) from e
        except OSError as e:
            raise WorkspaceError(
                f"Failed to run uv: {e}",
                hint="The uv binary may have been removed or is not executable.",
            ) from e
        return

    try:
        proc = subprocess.Popen(  # nosec B603
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as e:
        raise _uv_exec_error(uv, e) from e
    recent_lines: list[str] = []
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            stripped = line.rstrip()
            if stripped:
                add_line(stripped)
                recent_lines.append(stripped)
                if len(recent_lines) > 5:
                    recent_lines.pop(0)
        proc.wait()
        if proc.returncode != 0:
            detail = "\n".join(recent_lines) if recent_lines else ""
            raise WorkspaceError(
                "Failed to install dependencies.",
                hint=f"Last output:\n{detail}" if detail else None,
            )
    except WorkspaceError:
        raise
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        if proc.stdout:
            proc.stdout.close()


def run_list(uv: Path, venv_path: Path) -> subprocess.CompletedProcess:
    """List installed packages using uv pip list."""
    try:
        return subprocess.run(  # nosec B603
            [
                str(uv),
                "pip",
                "list",
                "--python",
                str(venv_path / "bin" / "python"),
                "--format=columns",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        raise WorkspaceError(f"Failed to list packages: {e.stderr.strip()}") from e
    except OSError as e:
        raise _uv_exec_error(uv, e) from e
