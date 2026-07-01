"""tlumi init - Initialize a new tlumi project."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from jinja2 import Environment, PackageLoader

from tlumi._safeio import safe_append_text, safe_chmod_dir, safe_mkdir, safe_write_text
from tlumi.config import CONFIG_FILE, TLUMI_DIR
from tlumi.display import (
    animated_status,
    console,
    install_live,
    print_banner,
    print_created_files,
    print_success,
    print_warning,
    prompt,
)
from tlumi.errors import ConfigError
from tlumi.uv import create_venv as uv_create_venv
from tlumi.uv import ensure_uv
from tlumi.uv import run_install as uv_install


def _render_template(env: Environment, template_name: str, **kwargs: str) -> str:
    tmpl = env.get_template(template_name)
    return tmpl.render(**kwargs)


def run_init(project_dir: Path | None = None, name: str | None = None) -> None:
    """Initialize a new tlumi project in the given directory."""
    if project_dir is None:
        project_dir = Path.cwd()
    project_dir = project_dir.resolve()

    config_exists = (project_dir / CONFIG_FILE).exists()
    tlumi_dir = project_dir / TLUMI_DIR

    if tlumi_dir.is_symlink():
        raise ConfigError(
            f".tlumi is a symlink to {tlumi_dir.resolve()}",
            hint="Remove the symlink before initializing. "
            "A malicious repository may have created it.",
        )

    venv_exists = (tlumi_dir / "cache" / "venv" / "bin" / "python").exists()

    # The project name is only consumed when rendering templates for a brand
    # new project. Already-configured projects keep the name recorded in
    # tlumi.yaml, so an oddly named checkout directory (e.g. '01-infra.prod')
    # must not block their setup. Validate before creating anything so a fresh
    # init with an invalid name fails without leaving a .tlumi/ behind.
    if not config_exists:
        if name is None:
            name = project_dir.name
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9_-]*$", name):
            raise ConfigError(
                f"Invalid project name: '{name}'",
                hint="Name must start with a letter and contain only letters, digits,"
                " hyphens, and underscores. Use --name to override.",
            )

    # Create directories and enforce 0o700 on EVERY init, including re-init on
    # a fully initialized project, so lax permissions from older versions or
    # manual creation are always repaired (documented hardening behavior).
    try:
        safe_mkdir(tlumi_dir, mode=0o700)
        safe_chmod_dir(tlumi_dir, 0o700)
        for subdir in ("state", "cache", str(Path("cache") / "pulumi_home")):
            sub_path = tlumi_dir / subdir
            if sub_path.is_symlink():
                raise ConfigError(
                    f".tlumi/{subdir} is a symlink",
                    hint="Remove the symlink before initializing. "
                    "A malicious repository may have created it.",
                )
            safe_mkdir(sub_path, mode=0o700)
    except OSError as e:
        raise ConfigError(
            f"Cannot create .tlumi/ directories: {e}",
            hint="Check disk space and directory permissions.",
        ) from e

    # If fully initialized already, re-run the (idempotent) dependency install.
    # The venv is created BEFORE packages are installed, so a prior failed
    # install leaves an empty venv while still tripping venv_exists; a naive
    # early-return would falsely report success and send the user to 'tlumi plan'
    # (which then fails with missing-module errors). uv no-ops fast when the
    # requirements are already satisfied, and surfaces the real error otherwise.
    if config_exists and venv_exists:
        console.print()  # intentional visual gap above warning
        print_warning("Project already initialized.")
        req_file = project_dir / "requirements.txt"
        if req_file.exists():
            uv = ensure_uv()
            venv_path = tlumi_dir / "cache" / "venv"
            with install_live("  Verifying dependencies...") as add_line:
                uv_install(uv, venv_path, ["-r", str(req_file)], add_line=add_line)
        console.print()  # intentional visual gap before hint
        console.print("  Run 'tlumi plan' to preview changes.")
        return

    # If tlumi.yaml exists but .tlumi/ is missing, this is a cloned/existing
    # project that just needs setup (venv, state dirs, deps install).
    existing_project = config_exists and not venv_exists

    if existing_project:
        print_banner("Setting up existing tlumi project...")
    else:
        print_banner("Initializing tlumi project...")

    created = []

    if not existing_project:
        # tlumi.yaml was absent on this path (fully initialized projects
        # returned above), so the name was derived and validated earlier.
        assert name is not None  # nosec B101

        # Default to secure (allow_unencrypted: false) in non-interactive runs so
        # automation pipelines don't silently scaffold plaintext-secrets projects.
        # Interactive runs explicitly opt the user in or out.
        allow_unencrypted = "false"
        if sys.stdin.isatty():
            console.print()
            console.print("  [info]Secret encryption[/info]")
            console.print("  Secrets can be encrypted via TLUMI_SECRETS_PASSPHRASE env var.")
            # Use prompt() (not raw console.input) so the window Live pauses
            # during input; otherwise the refresh thread erases the question.
            response = prompt("  Set up encryption now? (recommended) [Y/n]: ")
            normalized = response.strip().lower()
            # Empty (just Enter) keeps the secure default; explicit 'n'/'no' opts out.
            if normalized in ("n", "no"):
                allow_unencrypted = "true"
                console.print()
                console.print(
                    "  [warning]Secrets will be stored unencrypted in state."
                    " Set 'secrets.allow_unencrypted: false' in tlumi.yaml to revert.[/warning]"
                )
            else:
                console.print()
                console.print(
                    "  Set [bold]TLUMI_SECRETS_PASSPHRASE[/bold] before running plan/apply."
                )
        else:
            # Non-interactive runs keep the secure default, but the scaffolded
            # config then requires a passphrase; say so up front instead of
            # letting the very first 'tlumi plan' fail without warning.
            console.print()
            console.print(
                "  Set [bold]TLUMI_SECRETS_PASSPHRASE[/bold] before running plan/apply,"
                " or set 'secrets.allow_unencrypted: true' in tlumi.yaml."
            )

        # Render templates for new projects only
        jinja_env = Environment(  # nosec B701 - YAML/Python templates, not HTML
            loader=PackageLoader("tlumi", "templates"),
            keep_trailing_newline=True,
        )

        tlumi_yaml = _render_template(
            jinja_env, "tlumi.yaml.j2", project_name=name, allow_unencrypted=allow_unencrypted
        )
        infra_py = _render_template(jinja_env, "infra.py.j2", project_name=name)
        requirements = _render_template(jinja_env, "requirements.txt.j2")
        gitignore = _render_template(jinja_env, "gitignore.j2")

        try:
            config_path = project_dir / CONFIG_FILE
            if config_path.is_symlink():
                raise ConfigError(
                    f"{CONFIG_FILE} is a symlink to {config_path.resolve()}",
                    hint="Remove the symlink before initializing. "
                    "A malicious repository may have created it.",
                )
            safe_write_text(config_path, tlumi_yaml)
            created.append(f"{CONFIG_FILE:<24}project configuration")

            infra_path = project_dir / "infra.py"
            if infra_path.is_symlink():
                raise ConfigError(
                    f"infra.py is a symlink to {infra_path.resolve()}",
                    hint="Remove the symlink before initializing. "
                    "A malicious repository may have created it.",
                )
            if not infra_path.exists():
                safe_write_text(infra_path, infra_py)
                created.append(f"{'infra.py':<24}infrastructure definitions (edit this!)")

            req_path = project_dir / "requirements.txt"
            if req_path.is_symlink():
                raise ConfigError(
                    f"requirements.txt is a symlink to {req_path.resolve()}",
                    hint="Remove the symlink before initializing. "
                    "A malicious repository may have created it.",
                )
            if not req_path.exists():
                safe_write_text(req_path, requirements)
                created.append(f"{'requirements.txt':<24}Python dependencies")

            gitignore_path = project_dir / ".gitignore"
            if gitignore_path.is_symlink():
                print_warning(".gitignore is a symlink; skipping.")
            elif gitignore_path.exists():
                try:
                    existing = gitignore_path.read_text()
                except UnicodeDecodeError:
                    print_warning(".gitignore has unreadable encoding; skipping update.")
                    existing = None
                if existing is not None and ".tlumi/" not in existing:
                    safe_append_text(gitignore_path, "\n" + gitignore)
                    created.append(f"{'.gitignore':<24}updated with .tlumi/")
            else:
                safe_write_text(gitignore_path, gitignore)
                created.append(f"{'.gitignore':<24}git ignore rules")
        except OSError as e:
            raise ConfigError(
                f"Failed to write project files: {e}",
                hint="Check disk space and directory permissions.",
            ) from e

    created.append(f"{'.tlumi/':<24}internal state and cache")

    console.print()
    print_created_files(created)
    console.print()

    # Ensure uv is available
    uv = ensure_uv()

    # Create venv
    with animated_status("  Creating virtual environment..."):
        venv_path = tlumi_dir / "cache" / "venv"
        uv_create_venv(uv, venv_path)

    # Install base dependencies
    req_file = project_dir / "requirements.txt"
    if req_file.exists():
        with install_live("  Installing dependencies...") as add_line:
            uv_install(uv, venv_path, ["-r", str(req_file)], add_line=add_line)
    elif existing_project:
        print_warning("No requirements.txt found. Run 'tlumi deps add' to install providers.")

    if existing_project:
        print_success("Project setup complete!")
        console.print()  # intentional visual gap before next steps
        console.print("  Next steps:")
        console.print("    1. Preview changes: [bold]tlumi plan[/bold]")
        console.print("    2. Apply changes:   [bold]tlumi apply[/bold]")
    else:
        print_success("Project initialized successfully!")
        console.print()  # intentional visual gap before next steps
        console.print("  Next steps:")
        console.print("    1. Edit [bold]infra.py[/bold] to define your infrastructure")
        console.print("    2. Add providers:  [bold]tlumi deps add pulumi-aws[/bold]")
        console.print("    3. Preview changes: [bold]tlumi plan[/bold]")
        console.print("    4. Apply changes:   [bold]tlumi apply[/bold]")
