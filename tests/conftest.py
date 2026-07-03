"""Test-suite fixtures and environment hardening.

Rich decides whether to emit ANSI color by consulting ``FORCE_COLOR`` /
``CLICOLOR_FORCE`` before it falls back to TTY detection. CI runners
(GitHub Actions) and many interactive shells export ``FORCE_COLOR``, which
makes Rich style output even when stdout is a pytest capture buffer (a
non-TTY). Most assertions in this suite check for plain substrings
("1 to import", "+1", "resource(s) in state"), so forced color splits those
literals with escape codes and the assertions fail spuriously.

Removing the color-forcing variables restores Rich's natural behavior:
color for real terminals, plain text for captured/redirected output. Tests
that specifically need styled output build their own ``Console`` with
``force_terminal=True`` and are unaffected.
"""

import os

for _var in ("FORCE_COLOR", "CLICOLOR_FORCE"):
    os.environ.pop(_var, None)
