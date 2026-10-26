"""Suite-wide hooks.

R16: when ``CREDIT_TEST_DATABASE_URL`` is set, the whole suite
runs against Postgres instead of the test doubles — see
tests/pg_parity.py. Activation happens after collection, when
every test module has imported its store names; the tests then
build Postgres stores without knowing it.
"""

from __future__ import annotations

import pg_parity


def pytest_collection_modifyitems(session, config, items):
    pg_parity.activate()


def pytest_sessionfinish(session, exitstatus):
    pg_parity.cleanup()
