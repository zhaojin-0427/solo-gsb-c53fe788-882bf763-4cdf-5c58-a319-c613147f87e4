"""Seed demo projects on first launch.

Seeds the demo project (references + interruption), an empty project
(ready-made port target) and a long-chain project: many absolute items
created first, then the earlier items rewritten one by one to reference a
later item's end — a legal reference chain far longer than Python's
recursion limit, which must save/display/evaluate/port like any other.
"""

from __future__ import annotations

from . import db
from .engine import Item

LONG_CHAIN_LEN = 520  # ~500 levels: the reported legal long chain


def seed_if_empty() -> int | None:
    with db.transaction() as conn:
        if db.list_projects(conn):
            return None
        pid = db.create_project(conn, "演示工程")
        order = 0

        def add(**kw):
            nonlocal order
            order += 1
            return db.insert_item(conn, pid, Item(id=None, **kw), order)

        i1 = add(fixture="面光1", channel=1, target=180, duration=2000,
                 start_mode="abs", start_ms=0)
        add(fixture="面光1", channel=2, target=120, duration=1000,
            start_mode="abs", start_ms=500)
        i3 = add(fixture="顶光1", channel=1, target=255, duration=0,
                 start_mode="abs", start_ms=0)
        # starts when the first fade ends
        add(fixture="面光1", channel=1, target=60, duration=1500,
            start_mode="ref", ref_id=i1, ref_edge="end", ref_offset=0)
        # starts 200ms after the instant snap
        add(fixture="顶光1", channel=1, target=100, duration=800,
            start_mode="ref", ref_id=i3, ref_edge="start", ref_offset=200)
        i6 = add(fixture="面光1", channel=2, target=255, duration=4000,
                 start_mode="ref", ref_id=i1, ref_edge="start", ref_offset=1000)
        # this interrupts the long fade of i6
        add(fixture="面光1", channel=2, target=0, duration=500,
            start_mode="ref", ref_id=i6, ref_edge="start", ref_offset=1500)
        # an empty second project: a ready-made target for trying the
        # cross-project fragment port without creating one by hand
        db.create_project(conn, "空工程")
        _seed_long_chain(conn)
        return pid


def _seed_long_chain(conn) -> int:
    """Create the legal long reference chain project.

    All items start absolute first (seed writes bypass validation, so the
    provisional same-instant rows are never read as a project state); the
    earlier items are then rewritten in order to reference the *next*
    (later-id) item's end.  The final graph is one 520-level chain on a
    single channel — the root (last id) starts at 0ms and every earlier id
    starts 10ms after the later id it references, so item 1 starts at
    (n-1)*10 ms.
    """
    pid = db.create_project(conn, "长链工程")
    n = LONG_CHAIN_LEN
    ids = []
    for k in range(n):
        ids.append(db.insert_item(
            conn, pid,
            Item(id=None, fixture="长链", channel=1,
                 target=(k * 7) % 256, duration=10,
                 start_mode="abs", start_ms=0),
            k + 1))
    for k in range(n - 1):
        db.update_item(
            conn, pid,
            Item(id=ids[k], fixture="长链", channel=1,
                 target=(k * 7) % 256, duration=10,
                 start_mode="ref", ref_id=ids[k + 1],
                 ref_edge="end", ref_offset=0))
    return pid
