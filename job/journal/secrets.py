"""Secrets, resolved through exactly one indirection (SPEC §13.4, §13.7 seam 2).

The job holds exactly one secret: the IBKR Flex token. Every caller goes
through :func:`resolve_secret` — no code reads an environment variable, a
keychain or a file path for a secret directly. Swapping the v1 environment
backend for a keychain or 1Password in v2 is then a change to this one function
body, a config change rather than a code change anywhere else. Nothing
macOS-specific lives here, so the job path stays portable.
"""

from __future__ import annotations

import os

# The one secret the job holds (SPEC §13.4), scoped and rotatable, expiring
# 2027-07-14. Named, never spelled out at call sites.
IBKR_FLEX_TOKEN = "ibkr_flex_token"


class SecretNotFound(KeyError):
    """A named secret is not resolvable through the configured backend."""


def _env_key(name: str) -> str:
    return "JOURNAL_SECRET_" + name.upper()


def resolve_secret(name: str) -> str:
    """Return the value of the named secret, or raise :class:`SecretNotFound`.

    The single indirection: a logical name (``ibkr_flex_token``) maps to a
    backend lookup. v1 backend is the process environment
    (``JOURNAL_SECRET_IBKR_FLEX_TOKEN``).
    """
    value = os.environ.get(_env_key(name))
    if value is None:
        raise SecretNotFound(
            f"secret {name!r} is not set (expected env var {_env_key(name)})"
        )
    return value
