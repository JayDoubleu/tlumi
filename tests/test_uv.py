"""Tests for tlumi.uv: uv binary management."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tlumi.errors import WorkspaceError
from tlumi.uv import (
    _UV_VERSION,
    _download_uv,
    _find_uv,
    _link_mode_args,
    _platform_target,
    _shared_uv_cache_dir,
    _stat_device,
    _uv_cache_device,
    _verify_checksum,
    ensure_uv,
    run_install,
)


def test_shared_uv_cache_dir_includes_version():
    cache = _shared_uv_cache_dir()
    assert _UV_VERSION in str(cache)
    assert cache == Path.home() / ".tlumi" / "cache" / "uv" / _UV_VERSION


def test_find_uv_returns_none_when_not_on_path():
    with patch("tlumi.uv.shutil.which", return_value=None):
        assert _find_uv() is None


def test_find_uv_returns_path_when_found():
    with patch("tlumi.uv.shutil.which", return_value="/usr/bin/uv"):
        result = _find_uv()
        assert result == Path("/usr/bin/uv")


def test_platform_target_linux_x86():
    with (
        patch("tlumi.uv.platform.system", return_value="Linux"),
        patch("tlumi.uv.platform.machine", return_value="x86_64"),
        patch("tlumi.uv._is_musl_libc", return_value=False),
    ):
        assert _platform_target() == "x86_64-unknown-linux-gnu"


def test_platform_target_linux_aarch64():
    with (
        patch("tlumi.uv.platform.system", return_value="Linux"),
        patch("tlumi.uv.platform.machine", return_value="aarch64"),
        patch("tlumi.uv._is_musl_libc", return_value=False),
    ):
        assert _platform_target() == "aarch64-unknown-linux-gnu"


def test_platform_target_linux_musl_x86():
    """On musl (Alpine), select the statically-linked -musl build, not -gnu."""
    with (
        patch("tlumi.uv.platform.system", return_value="Linux"),
        patch("tlumi.uv.platform.machine", return_value="x86_64"),
        patch("tlumi.uv._is_musl_libc", return_value=True),
    ):
        assert _platform_target() == "x86_64-unknown-linux-musl"


def test_platform_target_linux_musl_aarch64():
    with (
        patch("tlumi.uv.platform.system", return_value="Linux"),
        patch("tlumi.uv.platform.machine", return_value="aarch64"),
        patch("tlumi.uv._is_musl_libc", return_value=True),
    ):
        assert _platform_target() == "aarch64-unknown-linux-musl"


def test_uv_exec_error_detects_libc_mismatch(tmp_path):
    """FileNotFoundError on an existing, executable uv binary points at glibc/musl."""
    from tlumi.uv import _uv_exec_error

    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\n")
    uv.chmod(0o700)
    err = _uv_exec_error(uv, FileNotFoundError(2, "No such file or directory"))
    assert "glibc/musl" in (err.hint or "")

    # A genuinely missing binary keeps the original generic hint.
    missing = _uv_exec_error(tmp_path / "gone", FileNotFoundError(2, "nope"))
    assert "removed or is not executable" in (missing.hint or "")


def test_platform_target_macos_arm():
    with (
        patch("tlumi.uv.platform.system", return_value="Darwin"),
        patch("tlumi.uv.platform.machine", return_value="arm64"),
    ):
        assert _platform_target() == "aarch64-apple-darwin"


def test_platform_target_macos_x86():
    with (
        patch("tlumi.uv.platform.system", return_value="Darwin"),
        patch("tlumi.uv.platform.machine", return_value="x86_64"),
    ):
        assert _platform_target() == "x86_64-apple-darwin"


def test_platform_target_unsupported():
    with (
        patch("tlumi.uv.platform.system", return_value="Windows"),
        patch("tlumi.uv.platform.machine", return_value="AMD64"),
    ):
        with pytest.raises(WorkspaceError, match="Unsupported platform"):
            _platform_target()


def test_ensure_uv_prefers_path():
    with patch("tlumi.uv._find_uv", return_value=Path("/usr/bin/uv")):
        assert ensure_uv() == Path("/usr/bin/uv")


def test_ensure_uv_uses_cache(tmp_path):
    cached_uv = tmp_path / "uv"
    cached_uv.write_text("fake")
    cached_uv.chmod(0o755)  # a real cached uv binary is executable

    with (
        patch("tlumi.uv._find_uv", return_value=None),
        patch("tlumi.uv._shared_uv_cache_dir", return_value=tmp_path),
    ):
        assert ensure_uv() == cached_uv


def test_ensure_uv_downloads_when_not_cached(tmp_path):
    dest = tmp_path / "download"

    with (
        patch("tlumi.uv._find_uv", return_value=None),
        patch("tlumi.uv._shared_uv_cache_dir", return_value=tmp_path),
        patch("tlumi.uv._download_uv", return_value=dest / "uv") as mock_dl,
    ):
        result = ensure_uv()
        assert result == dest / "uv"
        mock_dl.assert_called_once_with(tmp_path)


# ---------------------------------------------------------------------------
# _verify_checksum
# ---------------------------------------------------------------------------


def test_verify_checksum_passes_on_match(tmp_path):
    archive = tmp_path / "test.tar.gz"
    archive.write_bytes(b"hello world")
    expected = hashlib.sha256(b"hello world").hexdigest()

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_response = mock_urlopen.return_value.__enter__.return_value
        mock_response.read.return_value = expected.encode()
        # Should not raise
        _verify_checksum("https://example.com/test.tar.gz", archive, "test.tar.gz")


def test_verify_checksum_raises_on_mismatch(tmp_path):
    archive = tmp_path / "test.tar.gz"
    archive.write_bytes(b"hello world")

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_response = mock_urlopen.return_value.__enter__.return_value
        mock_response.read.return_value = (
            b"0000000000000000000000000000000000000000000000000000000000000000"
        )
        with pytest.raises(WorkspaceError, match="SHA-256 mismatch"):
            _verify_checksum("https://example.com/test.tar.gz", archive, "test.tar.gz")
    # Archive should be deleted on mismatch
    assert not archive.exists()


def test_verify_checksum_raises_on_download_failure(tmp_path):
    archive = tmp_path / "test.tar.gz"
    archive.write_bytes(b"hello world")

    with patch("urllib.request.urlopen", side_effect=OSError("not found")):
        with pytest.raises(WorkspaceError, match="Failed to download checksum file"):
            _verify_checksum("https://example.com/test.tar.gz", archive, "test.tar.gz")
    # Archive should be deleted on checksum fetch failure
    assert not archive.exists()


def test_verify_checksum_raises_on_timeout(tmp_path):
    archive = tmp_path / "test.tar.gz"
    archive.write_bytes(b"hello world")

    with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        with pytest.raises(WorkspaceError, match="Failed to download checksum file"):
            _verify_checksum("https://example.com/test.tar.gz", archive, "test.tar.gz")
    assert not archive.exists()


# ---------------------------------------------------------------------------
# _download_uv
# ---------------------------------------------------------------------------


def test_download_uv_timeout_raises_workspace_error(tmp_path):
    dest = tmp_path / "dest"

    with (
        patch("tlumi.uv._platform_target", return_value="x86_64-unknown-linux-gnu"),
        patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")),
    ):
        with pytest.raises(WorkspaceError, match="Failed to download uv"):
            _download_uv(dest)


def test_download_uv_cleans_partial_archive_on_failure(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir(parents=True)
    target = "x86_64-unknown-linux-gnu"
    archive_name = f"uv-{target}.tar.gz"

    with (
        patch("tlumi.uv._platform_target", return_value=target),
        patch("urllib.request.urlopen", side_effect=OSError("connection refused")),
    ):
        with pytest.raises(WorkspaceError):
            _download_uv(dest)
    # Archive should not be left behind on download failure
    assert not (dest / archive_name).exists()


def test_download_uv_extracts_binary(tmp_path):
    """Build a real tar.gz with a uv binary and verify extraction."""
    dest = tmp_path / "dest"
    target = "x86_64-unknown-linux-gnu"
    archive_name = f"uv-{target}.tar.gz"

    # Create a tar.gz archive containing <target>/uv
    archive_buf = io.BytesIO()
    with tarfile.open(fileobj=archive_buf, mode="w:gz") as tar:
        uv_content = b"#!/bin/sh\necho fake-uv"
        info = tarfile.TarInfo(name=f"uv-{target}/uv")
        info.size = len(uv_content)
        tar.addfile(info, io.BytesIO(uv_content))
    archive_bytes = archive_buf.getvalue()

    checksum = hashlib.sha256(archive_bytes).hexdigest()

    def fake_urlopen(url, **_kwargs):
        if url.endswith(".sha256"):
            return io.BytesIO(f"{checksum}  {archive_name}\n".encode())
        return io.BytesIO(archive_bytes)

    with (
        patch("tlumi.uv._platform_target", return_value=target),
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
    ):
        result = _download_uv(dest)

    assert result == dest / "uv"
    assert result.exists()
    assert result.read_bytes() == b"#!/bin/sh\necho fake-uv"


# ---------------------------------------------------------------------------
# _download_uv: extraction edge cases
# ---------------------------------------------------------------------------


def _make_uv_archive(target: str, content: bytes = b"#!/bin/sh\necho fake-uv") -> bytes:
    """Build a tar.gz archive containing <target>/uv with given content."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name=f"uv-{target}/uv")
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def test_download_uv_corrupted_archive(tmp_path):
    """TarError during extraction raises WorkspaceError."""
    dest = tmp_path / "dest"
    target = "x86_64-unknown-linux-gnu"

    def fake_urlopen(url, **kwargs):
        return io.BytesIO(b"not a valid tar.gz at all")

    with (
        patch("tlumi.uv._platform_target", return_value=target),
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
        patch("tlumi.uv._verify_checksum"),
    ):
        with pytest.raises(WorkspaceError, match="Failed to extract"):
            _download_uv(dest)


def test_download_uv_missing_binary_in_archive(tmp_path):
    """Archive with no /uv file raises WorkspaceError."""
    dest = tmp_path / "dest"
    target = "x86_64-unknown-linux-gnu"

    # Create a valid tar.gz without a uv binary
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        content = b"some other file"
        info = tarfile.TarInfo(name="README.txt")
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    archive_bytes = buf.getvalue()

    def fake_urlopen(url, **kwargs):
        return io.BytesIO(archive_bytes)

    with (
        patch("tlumi.uv._platform_target", return_value=target),
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
        patch("tlumi.uv._verify_checksum"),
    ):
        with pytest.raises(WorkspaceError, match="uv binary not found"):
            _download_uv(dest)


def test_download_uv_archive_cleaned_after_extraction(tmp_path):
    """Archive file is deleted after successful extraction."""
    dest = tmp_path / "dest"
    target = "x86_64-unknown-linux-gnu"
    archive_name = f"uv-{target}.tar.gz"
    archive_bytes = _make_uv_archive(target)

    def fake_urlopen(url, **kwargs):
        return io.BytesIO(archive_bytes)

    with (
        patch("tlumi.uv._platform_target", return_value=target),
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
        patch("tlumi.uv._verify_checksum"),
    ):
        _download_uv(dest)

    assert not (dest / archive_name).exists()


def test_download_uv_sets_executable_permission(tmp_path):
    """Extracted binary gets owner-only executable mode."""
    dest = tmp_path / "dest"
    target = "x86_64-unknown-linux-gnu"
    archive_bytes = _make_uv_archive(target)

    def fake_urlopen(url, **kwargs):
        return io.BytesIO(archive_bytes)

    with (
        patch("tlumi.uv._platform_target", return_value=target),
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
        patch("tlumi.uv._verify_checksum"),
    ):
        result = _download_uv(dest)

    assert result.stat().st_mode & 0o777 == 0o700


def test_ensure_uv_downloads_on_cache_miss(tmp_path):
    """Full flow: no PATH, no cache, triggers download and returns binary."""
    cache_dir = tmp_path / "cache"
    target = "x86_64-unknown-linux-gnu"
    archive_bytes = _make_uv_archive(target)

    def fake_urlopen(url, **kwargs):
        return io.BytesIO(archive_bytes)

    with (
        patch("tlumi.uv._find_uv", return_value=None),
        patch("tlumi.uv._shared_uv_cache_dir", return_value=cache_dir),
        patch("tlumi.uv._platform_target", return_value=target),
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
        patch("tlumi.uv._verify_checksum"),
    ):
        result = ensure_uv()

    assert result == cache_dir / "uv"
    assert result.exists()


# ---------------------------------------------------------------------------
# R4-T1: run_install() streaming mode non-zero exit
# ---------------------------------------------------------------------------


def test_run_install_streaming_nonzero_exit(tmp_path):
    """Streaming mode with non-zero exit raises WorkspaceError with recent lines."""
    from unittest.mock import MagicMock

    from tlumi.uv import run_install

    lines_collected: list[str] = []
    mock_stdout = MagicMock()
    mock_stdout.__iter__ = MagicMock(return_value=iter(["line 1\n", "line 2\n", "error: fail\n"]))

    with (
        patch("tlumi.uv._link_mode_args", return_value=[]),
        patch("tlumi.uv.subprocess.Popen") as mock_popen,
    ):
        mock_proc = mock_popen.return_value
        mock_proc.stdout = mock_stdout
        mock_proc.wait.return_value = 1
        mock_proc.returncode = 1

        with pytest.raises(WorkspaceError, match="Failed to install") as exc_info:
            run_install(Path("/fake/uv"), tmp_path, ["pkg"], add_line=lines_collected.append)

        assert "error: fail" in (exc_info.value.hint or "")


# ---------------------------------------------------------------------------
# R4-T2: run_install() KeyboardInterrupt cleanup
# ---------------------------------------------------------------------------


def test_run_install_keyboard_interrupt_cleanup(tmp_path):
    """KeyboardInterrupt during streaming kills process and closes stdout."""
    from unittest.mock import MagicMock

    from tlumi.uv import run_install

    def raise_on_second():
        yield "line 1\n"
        raise KeyboardInterrupt()

    mock_stdout = MagicMock()
    mock_stdout.__iter__ = MagicMock(return_value=raise_on_second())

    with (
        patch("tlumi.uv._link_mode_args", return_value=[]),
        patch("tlumi.uv.subprocess.Popen") as mock_popen,
    ):
        mock_proc = mock_popen.return_value
        mock_proc.stdout = mock_stdout
        mock_proc.wait.return_value = 0
        mock_proc.returncode = 0

        with pytest.raises(KeyboardInterrupt):
            run_install(Path("/fake/uv"), tmp_path, ["pkg"], add_line=lambda x: None)

        mock_proc.kill.assert_called_once()
        mock_stdout.close.assert_called_once()


# ---------------------------------------------------------------------------
# R4-T3: _verify_checksum() malformed checksum data
# ---------------------------------------------------------------------------


def test_verify_checksum_malformed_non_utf8(tmp_path):
    """Non-UTF-8 checksum data raises WorkspaceError and deletes archive."""
    archive = tmp_path / "test.tar.gz"
    archive.write_bytes(b"hello world")

    with patch("urllib.request.urlopen") as mock_urlopen:
        # Return invalid bytes that can't be decoded as UTF-8
        mock_response = mock_urlopen.return_value.__enter__.return_value
        mock_response.read.return_value = b"\xff\xfe invalid utf8"
        with pytest.raises(WorkspaceError, match="Failed to parse checksum"):
            _verify_checksum("https://example.com/test.tar.gz", archive, "test.tar.gz")
    assert not archive.exists()


def test_verify_checksum_empty_response(tmp_path):
    """Empty checksum response raises WorkspaceError and deletes archive."""
    archive = tmp_path / "test.tar.gz"
    archive.write_bytes(b"hello world")

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_response = mock_urlopen.return_value.__enter__.return_value
        mock_response.read.return_value = b""
        with pytest.raises(WorkspaceError, match="Failed to parse checksum|SHA-256 mismatch"):
            _verify_checksum("https://example.com/test.tar.gz", archive, "test.tar.gz")
    assert not archive.exists()


# ---------------------------------------------------------------------------
# T9: run_install quiet OSError (no add_line)
# ---------------------------------------------------------------------------


def test_run_install_quiet_oserror(tmp_path):
    """run_install without add_line raises WorkspaceError on OSError."""
    from tlumi.uv import run_install

    with patch("tlumi.uv.subprocess.Popen", side_effect=OSError("not found")):
        with pytest.raises(WorkspaceError, match="Failed to run uv"):
            run_install(Path("/fake/uv"), tmp_path, ["pkg"])


def test_download_uv_path_traversal_member(tmp_path):
    """Archive with ../evil/uv member extracts safely (name overridden to 'uv')."""
    target = "x86_64-unknown-linux-gnu"

    # Create an archive with a path traversal member
    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="../evil/uv")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"evil"))
    archive_bytes.seek(0)

    def fake_urlopen(url, **kwargs):
        return io.BytesIO(archive_bytes.getvalue())

    cache = tmp_path / "cache"
    with (
        patch("tlumi.uv._platform_target", return_value=target),
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
        patch("tlumi.uv._verify_checksum"),
    ):
        # The code overrides member.name to "uv" before extraction,
        # so path traversal members are neutralized
        result = _download_uv(cache)

    # Binary ends up safely inside dest_dir
    assert result == cache / "uv"
    assert result.exists()
    # No file created outside the dest_dir
    assert not (tmp_path / "evil").exists()


# ---------------------------------------------------------------------------
# F13/F14/F22: cache robustness (atomic write, poison self-heal, perms)
# ---------------------------------------------------------------------------


def test_ensure_uv_ignores_non_executable_cache(tmp_path):
    """A non-executable cached binary (poisoned by an interrupted run) is re-downloaded (F14)."""
    cached_uv = tmp_path / "uv"
    cached_uv.write_text("partial")
    cached_uv.chmod(0o644)  # present and non-empty, but not executable
    sentinel = tmp_path / "fresh-uv"

    with (
        patch("tlumi.uv._find_uv", return_value=None),
        patch("tlumi.uv._shared_uv_cache_dir", return_value=tmp_path),
        patch("tlumi.uv._download_uv", return_value=sentinel) as mock_dl,
    ):
        assert ensure_uv() == sentinel
        mock_dl.assert_called_once_with(tmp_path)


def test_ensure_uv_ignores_empty_cache(tmp_path):
    """An empty cached binary is re-downloaded rather than returned (F14)."""
    cached_uv = tmp_path / "uv"
    cached_uv.write_bytes(b"")
    cached_uv.chmod(0o755)
    sentinel = tmp_path / "fresh-uv"

    with (
        patch("tlumi.uv._find_uv", return_value=None),
        patch("tlumi.uv._shared_uv_cache_dir", return_value=tmp_path),
        patch("tlumi.uv._download_uv", return_value=sentinel) as mock_dl,
    ):
        assert ensure_uv() == sentinel
        mock_dl.assert_called_once_with(tmp_path)


def test_make_cache_dir_tightens_permissions(tmp_path, monkeypatch):
    """_make_cache_dir chmods every level under ~/.tlumi to 0o700, even pre-existing ones (F22)."""
    from tlumi.uv import _make_cache_dir

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Pre-create a loose parent to prove exist_ok still tightens it.
    (tmp_path / ".tlumi").mkdir(mode=0o755)
    dest = tmp_path / ".tlumi" / "cache" / "uv" / _UV_VERSION

    _make_cache_dir(dest)

    for level in (".tlumi", ".tlumi/cache", ".tlumi/cache/uv", f".tlumi/cache/uv/{_UV_VERSION}"):
        mode = (tmp_path / level).stat().st_mode & 0o777
        assert mode == 0o700, f"{level} is {oct(mode)}"


# ---------------------------------------------------------------------------
# run_list: success, non-zero exit, and exec failure (audit [52])
# ---------------------------------------------------------------------------


def _make_fake_uv(tmp_path, script: str) -> Path:
    """Create an executable fake uv shell script in tmp_path."""
    uv = tmp_path / "fake-uv"
    uv.write_text(f"#!/bin/sh\n{script}")
    uv.chmod(0o700)
    return uv


def test_run_list_returns_completed_process(tmp_path):
    """run_list invokes 'uv pip list --python <venv>/bin/python --format=columns'."""
    from tlumi.uv import run_list

    # Echo each argument on its own line so the invocation can be asserted.
    uv = _make_fake_uv(tmp_path, 'printf "%s\\n" "$@"\n')
    venv = tmp_path / "venv"

    result = run_list(uv, venv)

    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "pip",
        "list",
        "--python",
        str(venv / "bin" / "python"),
        "--format=columns",
    ]


def test_run_list_nonzero_exit_wraps_workspace_error(tmp_path):
    """A non-zero uv exit raises WorkspaceError carrying the stripped stderr."""
    from tlumi.uv import run_list

    uv = _make_fake_uv(tmp_path, 'echo "error: no interpreter found" >&2\nexit 2\n')

    with pytest.raises(WorkspaceError, match="Failed to list packages: error: no interpreter"):
        run_list(uv, tmp_path / "venv")


def test_run_list_missing_binary_wraps_workspace_error(tmp_path):
    """A missing uv binary (OSError on exec) raises WorkspaceError with a hint."""
    from tlumi.uv import run_list

    with pytest.raises(WorkspaceError, match="Failed to run uv") as exc_info:
        run_list(tmp_path / "gone-uv", tmp_path / "venv")

    assert "removed or is not executable" in (exc_info.value.hint or "")


def test_download_uv_cleans_temp_on_replace_failure(tmp_path):
    """A failure during the atomic replace leaves no partial binary or temp file (F13/F14)."""
    dest = tmp_path / "dest"
    target = "x86_64-unknown-linux-gnu"
    archive_bytes = _make_uv_archive(target)

    def fake_urlopen(url, **kwargs):
        return io.BytesIO(archive_bytes)

    with (
        patch("tlumi.uv._platform_target", return_value=target),
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
        patch("tlumi.uv._verify_checksum"),
        patch("tlumi.uv.os.replace", side_effect=OSError("disk full")),
    ):
        with pytest.raises(WorkspaceError, match="Failed to install uv binary"):
            _download_uv(dest)

    assert not (dest / "uv").exists()  # cache not poisoned
    assert list(dest.glob("uv.*.tmp")) == []  # temp cleaned up


# ---------------------------------------------------------------------------
# http.client.HTTPException (IncompleteRead) is not an OSError subclass, so a
# connection dropped mid-body must still surface as WorkspaceError, not a raw
# traceback.
# ---------------------------------------------------------------------------


def test_verify_checksum_incomplete_read_raises_workspace_error(tmp_path):
    """A connection dropped mid-body while reading the .sha256 file (IncompleteRead)
    surfaces as WorkspaceError and deletes the archive (docstring cleanup contract)."""
    import http.client

    archive = tmp_path / "test.tar.gz"
    archive.write_bytes(b"hello world")

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_response = mock_urlopen.return_value.__enter__.return_value
        mock_response.read.side_effect = http.client.IncompleteRead(b"partial", 90)
        with pytest.raises(WorkspaceError, match="Failed to download checksum file"):
            _verify_checksum("https://example.com/test.tar.gz", archive, "test.tar.gz")
    assert not archive.exists()


def test_download_uv_incomplete_read_raises_workspace_error(tmp_path):
    """A connection dropped mid-body while downloading the archive (IncompleteRead)
    surfaces as WorkspaceError and leaves no archive behind."""
    import http.client

    dest = tmp_path / "dest"
    target = "x86_64-unknown-linux-gnu"

    with (
        patch("tlumi.uv._platform_target", return_value=target),
        patch("urllib.request.urlopen") as mock_urlopen,
    ):
        response = mock_urlopen.return_value.__enter__.return_value
        response.read.side_effect = http.client.IncompleteRead(b"", 100)
        with pytest.raises(WorkspaceError, match="Failed to download uv"):
            _download_uv(dest)

    assert list(dest.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# Concurrent first-run downloads must use a pid-unique archive path so they do
# not truncate each other (which would trip a false checksum mismatch).
# ---------------------------------------------------------------------------


def test_download_uv_archive_path_is_pid_unique(tmp_path):
    """The downloaded archive lands at a pid-unique path, not the shared
    dest/filename, so parallel first-run downloads do not collide."""
    dest = tmp_path / "dest"
    target = "x86_64-unknown-linux-gnu"
    archive_bytes = _make_uv_archive(target)

    captured: dict[str, Path] = {}
    real_open = tarfile.open

    def spy_open(path, *args, **kwargs):
        captured["path"] = Path(path)
        return real_open(path, *args, **kwargs)

    def fake_urlopen(url, **kwargs):
        return io.BytesIO(archive_bytes)

    with (
        patch("tlumi.uv._platform_target", return_value=target),
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
        patch("tlumi.uv._verify_checksum"),
        patch("tlumi.uv.os.getpid", return_value=99999),
        patch("tlumi.uv.tarfile.open", side_effect=spy_open),
    ):
        _download_uv(dest)

    assert "99999" in captured["path"].name
    assert captured["path"] != dest / f"uv-{target}.tar.gz"


# uv --link-mode=copy suppression for cross-filesystem venv/cache


def test_stat_device_walks_up_to_existing_ancestor(tmp_path):
    """_stat_device returns the device of the nearest existing ancestor."""
    missing = tmp_path / "does" / "not" / "exist" / "venv"
    assert _stat_device(missing) == tmp_path.stat().st_dev


def test_stat_device_returns_none_when_nothing_stat_able():
    with patch("tlumi.uv.Path.stat", side_effect=OSError("boom")):
        assert _stat_device(Path("/whatever/venv")) is None


def test_uv_cache_device_none_on_missing_binary():
    """A uv binary that cannot be executed yields no device (no flag forced)."""
    assert _uv_cache_device(Path("/nonexistent/uv")) is None


def test_uv_cache_device_reads_uv_cache_dir(tmp_path):
    result = MagicMock(stdout=f"{tmp_path}\n")
    with patch("tlumi.uv.subprocess.run", return_value=result) as mock_run:
        dev = _uv_cache_device(Path("/fake/uv"))
    assert dev == tmp_path.stat().st_dev
    assert mock_run.call_args[0][0] == ["/fake/uv", "cache", "dir"]


def test_uv_cache_device_none_on_blank_output():
    """Blank `uv cache dir` output fails open to None (no flag forced)."""
    with patch("tlumi.uv.subprocess.run", return_value=MagicMock(stdout="   \n")):
        assert _uv_cache_device(Path("/fake/uv")) is None


def test_link_mode_copy_on_cross_filesystem():
    with (
        patch("tlumi.uv._uv_cache_device", return_value=1),
        patch("tlumi.uv._stat_device", return_value=2),
    ):
        assert _link_mode_args(Path("/fake/uv"), Path("/proj/.tlumi/cache/venv")) == [
            "--link-mode=copy"
        ]


def test_link_mode_empty_on_same_filesystem():
    with (
        patch("tlumi.uv._uv_cache_device", return_value=7),
        patch("tlumi.uv._stat_device", return_value=7),
    ):
        assert _link_mode_args(Path("/fake/uv"), Path("/proj/.tlumi/cache/venv")) == []


def test_link_mode_empty_when_cache_device_unknown():
    with (
        patch("tlumi.uv._uv_cache_device", return_value=None),
        patch("tlumi.uv._stat_device", return_value=2),
    ):
        assert _link_mode_args(Path("/fake/uv"), Path("/proj/.tlumi/cache/venv")) == []


def test_run_install_adds_copy_link_mode_when_cross_fs(tmp_path):
    """run_install threads the copy link mode in front of --python."""
    with (
        patch("tlumi.uv._link_mode_args", return_value=["--link-mode=copy"]),
        patch("tlumi.uv.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run,
    ):
        run_install(Path("/fake/uv"), tmp_path, ["pulumi-random"])
    cmd = mock_run.call_args[0][0]
    assert "--link-mode=copy" in cmd
    assert cmd.index("--link-mode=copy") < cmd.index("--python")
    assert cmd[-1] == "pulumi-random"


def test_run_install_omits_link_mode_when_same_fs(tmp_path):
    """No link-mode flag on the same filesystem (uv keeps its fast default)."""
    with (
        patch("tlumi.uv._link_mode_args", return_value=[]),
        patch("tlumi.uv.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run,
    ):
        run_install(Path("/fake/uv"), tmp_path, ["pulumi-random"])
    cmd = mock_run.call_args[0][0]
    assert "--link-mode" not in cmd
    assert all(not str(part).startswith("--link-mode") for part in cmd)
