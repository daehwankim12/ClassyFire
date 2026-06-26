"""Repo-root pytest configuration.

Hermetic auth-gate environment
------------------------------
The app runs in shared-access "locked" mode whenever ``SHARED_ACCESS_PASSWORD_HASH``
is non-empty (see ``apps.core.security._is_dev_open``). The test suite, like CI,
assumes *dev-open* mode by default — the locked-mode tests opt in explicitly via
``@override_settings(SHARED_ACCESS_PASSWORD_HASH=...)``.

A developer's shell (or a sourced ``.env``) may export ``SHARED_ACCESS_PASSWORD_HASH``;
without intervention that value leaks into the test process and flips the whole
app into locked mode, so every dev-open / API test fails with a spurious 401.

Clearing it here — at conftest import time, before Django settings are read — makes
test runs independent of the developer's environment. ``load_dotenv`` in
``config.settings.base`` uses ``override=False`` and ``.env`` does not define this
key, so this assignment is not clobbered.
"""

import os

# Must run before Django settings (config.settings.base) are imported.
os.environ["SHARED_ACCESS_PASSWORD_HASH"] = ""
