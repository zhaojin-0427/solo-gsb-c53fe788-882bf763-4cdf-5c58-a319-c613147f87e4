"""Seed demo projects on first launch (references, interruption, and a long
legal reference chain that used to overflow the engine's recursion)."""

from __future__ import annotations

from . import db
from .engine import Item

LONG_CHAIN_ITEMS = 520  # deeper than Python's old recursion budget


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
    """A legal reference chain hundreds of levels long.

    Built exactly like the UI repro: every item is inserted as an absolute
    cue first, then the earlier items are changed one by one to reference
    the *later* item's end + 1ms (earlier ids depend on later ids, so id
    order cannot topologically walk the chain).  Channels rotate so the
    resolved starts never collide on one lane.
    """
    pid = db.create_project(conn, f"长链工程（{LONG_CHAIN_ITEMS}级引用）")
    order = db.next_sort_order(conn, pid)
    n = LONG_CHAIN_ITEMS
    ids = []
    for i in range(1, n + 1):
        channel = ((i - 1) % 16) + 1
        it = Item(id=None, fixture="链灯", channel=channel, target=100,
                  duration=10, start_mode="abs", start_ms=i * 100)
        ids.append(db.insert_item(conn, pid, it, order))
        order += 1
    # rewire #1 -> #2(end)+1, #2 -> #3(end)+1, ... ; the last stays absolute
    for i in range(1, n):
        channel = ((i - 1) % 16) + 1
        db.update_item(conn, pid, Item(
            id=ids[i - 1], fixture="链灯", channel=channel, target=100,
            duration=10, start_mode="ref", ref_id=ids[i],
            ref_edge="end", ref_offset=1))
    return pid
