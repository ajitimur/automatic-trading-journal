"""Secrets resolve through the single indirection, with no hardcoded path."""

import os
import unittest

from journal import secrets


class SecretsTest(unittest.TestCase):
    def setUp(self):
        self.env_key = secrets._env_key(secrets.IBKR_FLEX_TOKEN)
        self._saved = os.environ.pop(self.env_key, None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(self.env_key, None)
        else:
            os.environ[self.env_key] = self._saved

    def test_resolves_through_env_indirection(self):
        os.environ[self.env_key] = "flex-token-123"
        self.assertEqual(
            secrets.resolve_secret(secrets.IBKR_FLEX_TOKEN), "flex-token-123"
        )

    def test_missing_secret_raises_clearly(self):
        with self.assertRaises(secrets.SecretNotFound):
            secrets.resolve_secret(secrets.IBKR_FLEX_TOKEN)

    def test_no_hardcoded_path_or_macos_call_in_secrets_module(self):
        # The job path must carry nothing macOS-specific (SPEC §13.7 seam 3)
        # and no hardcoded secret path (§13.4). Guard the resolver's source.
        import inspect

        source = inspect.getsource(secrets)
        for forbidden in ("security find-generic-password", "/usr/bin/security", "Keychain"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
