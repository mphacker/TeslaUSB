"""Scaffolding blueprints for Phase 5.4.

The B-1 ``base.html`` references several blueprint endpoints via
``url_for(...)``:

* ``mapping.map_view`` — full UI lands in Phase 5.13
* ``analytics.dashboard`` — Phase 5.7
* ``media.media_home`` — Phase 5.8 onward
* ``cloud_archive.index`` — Phase 5.14
* ``settings.index`` — Phase 5.16

Until those increments land, ``url_for`` would raise
``werkzeug.routing.BuildError`` at render time and the template
test would fail. We register **scaffolding** blueprints here
that own the endpoint *names* only. They serve a small "coming
in Phase X.Y" stub at the URL so the template renders cleanly
and a curious operator hitting the URL in development sees a
clear message instead of a 500.

Each subsequent Phase 5.N increment replaces the relevant
scaffold with the real blueprint **with the same blueprint name
and endpoint name**, so ``base.html`` need not change. The
``app.py`` factory checks ``app.blueprints`` before registering
a scaffold, so passing the real blueprint via
``create_app(..., extra_blueprints=[...])`` from a test will not
collide.

This module is **scaffolding only** — it MUST NOT grow business
logic. If you find yourself adding a non-trivial route here,
that route belongs in its own blueprint module instead.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from flask import Blueprint, current_app

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from flask.typing import ResponseReturnValue


@dataclasses.dataclass(frozen=True, slots=True)
class _Scaffold:
    name: str
    url_prefix: str
    endpoint: str  # the endpoint name base.html references
    phase: str  # future-phase label shown in the stub body


_SCAFFOLDS: tuple[_Scaffold, ...] = (
    _Scaffold("mapping", "/map", "map_view", "Phase 5.13"),
    _Scaffold("media", "/media", "media_home", "Phase 5.8 onward"),
    _Scaffold("cloud_archive", "/cloud", "index", "Phase 5.14"),
)


def _make_view(scaffold: _Scaffold) -> Callable[[], ResponseReturnValue]:
    """Return a closure-bound stub view that announces the future phase.

    The view's ``__name__`` is unique per scaffold so Flask's
    view-function registry doesn't collide if the function is
    referenced by name during error reporting.
    """

    def view() -> ResponseReturnValue:
        current_app.logger.info(
            "scaffold blueprint hit: blueprint=%s endpoint=%s future_phase=%s",
            scaffold.name,
            scaffold.endpoint,
            scaffold.phase,
        )
        body = (
            f"<!doctype html><title>{scaffold.name}</title>"
            f"<p>The <code>{scaffold.name}.{scaffold.endpoint}</code> blueprint "
            f"is scaffolding only. The full UI lands in {scaffold.phase}.</p>"
        )
        return body, 200

    view.__name__ = f"_{scaffold.name}_{scaffold.endpoint}_scaffold"
    return view


def build_scaffold_blueprints() -> Iterable[Blueprint]:
    """Return the scaffolding blueprints in registration order."""
    blueprints: list[Blueprint] = []
    for scaffold in _SCAFFOLDS:
        bp = Blueprint(scaffold.name, __name__, url_prefix=scaffold.url_prefix)
        bp.add_url_rule("/", endpoint=scaffold.endpoint, view_func=_make_view(scaffold))
        blueprints.append(bp)
    return blueprints
