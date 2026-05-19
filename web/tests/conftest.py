"""Shared pytest fixtures.

Phase 0.3 ships an empty conftest so pytest discovers the `tests/`
package without warnings. Real fixtures (Flask test client, fake
IPC socket, temp config dir) arrive in Phase 5.2 alongside the
Flask factory.
"""

from __future__ import annotations
