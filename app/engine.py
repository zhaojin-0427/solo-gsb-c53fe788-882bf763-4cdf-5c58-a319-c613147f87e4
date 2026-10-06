"""Pure timing / value engine for the lighting fade timeline.

An item describes a target value for one fixture channel.  Its start is
either an absolute time (ms) or a reference to another item's start/end
plus a non-negative offset.  The engine

* validates references (missing / cycles) and rejects two items of the
  same fixture/channel starting at the same resolved instant,
* resolves every item to an absolute start time in reference order,
* builds per-channel piecewise-linear fade segments, each fade starting
  from the *unrounded* value left by the previous fade (a later item can
  interrupt an earlier fade),
* evaluates the channel values at any non-negative time, including
  fractional milliseconds (``fractions.Fraction``), exactly; display
  values round half up.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, Iterable, List, Optional, Set, Tuple


class EngineError(Exception):
    """A validation failure. ``detail`` carries a Chinese user message."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class Item:
    id: Optional[int]
    fixture: str
    channel: int
    target: int
    duration: int
    start_mode: str  # "abs" | "ref"
    start_ms: int | None = None
    ref_id: int | None = None
    ref_edge: str | None = None  # "start" | "end"
    ref_offset: int | None = None


@dataclass
class Segment:
    begin: int  # segment occupies [begin, end) (instant items: begin == end)
    end: int
    start_value: Fraction
    end_value: Fraction
    instant: bool
    item_id: int


@dataclass
class ResolvedItem:
    item: Item
    start: int
    end: int
    interrupted_by: Optional[int]


@dataclass
class ResolvedView:
    items: List[ResolvedItem]  # reference order, then id
    lanes: Dict[str, List[Segment]]  # key "fixture|channel", start order


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def validate(items: Iterable[Item]) -> None:
    items = list(items)
    by_id = {it.id: it for it in items if it.id is not None}

    # 1. missing references
    for it in items:
        if it.start_mode == "ref":
            if it.ref_id not in by_id:
                raise EngineError(f"提示项 {it.ref_id} 不存在，引用缺失")

    # 2. reference cycles
    _check_cycles(items, by_id)

    # 3. two items of the same fixture/channel starting at the same instant
    starts = resolve_starts(items, by_id)
    starts_at: Dict[Tuple[str, int], set] = {}
    for it in items:
        key = (it.fixture, it.channel)
        s = starts[it.id]
        bucket = starts_at.setdefault(key, set())
        if s in bucket:
            raise EngineError(
                f"灯具 {it.fixture} 通道 {it.channel} 在 {s}ms 已有一项同时起步"
            )
        bucket.add(s)


def _check_cycles(items: List[Item], by_id: Dict[int, Item]) -> None:
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {it.id: WHITE for it in items if it.id is not None}

    for root in color:
        if color[root] != WHITE:
            continue
        # stack entries: (node, child iterator)
        stack: List[Tuple[int, Iterable[int]]] = [
            (root, iter([by_id[root].ref_id]) if by_id[root].start_mode == "ref" else iter(()))
        ]
        color[root] = GRAY
        while stack:
            node, children = stack[-1]
            advanced = False
            for child in children:
                advanced = True
                if color.get(child) == GRAY:
                    chain = [n for n, _ in stack]
                    i = chain.index(child)
                    path = " -> ".join("#" + str(n) for n in chain[i:] + [child])
                    raise EngineError(f"引用链存在环：{path}")
                if color.get(child) == WHITE:
                    color[child] = GRAY
                    cit = by_id[child]
                    stack.append(
                        (child, iter([cit.ref_id]) if cit.start_mode == "ref" else iter(()))
                    )
                    break
            if not advanced:
                color[node] = BLACK
                stack.pop()


def dependency_order(
    by_id: Dict[int, Item], roots: Optional[Iterable[int]] = None
) -> List[int]:
    """Return reference ancestors before their dependents (iterative).

    An item referencing another is ordered *after* that referenced item,
    so walking the result guarantees every ``ref_id`` has already been
    handled — the same order drives time resolution and the ancestor-first
    writes of cross-project porting.  Ties break by id for deterministic
    output.  Callers must already have ruled out missing references and
    cycles; when ``roots`` is given, only their transitive closure is
    returned.

    The traversal is explicit (a Kahn-style heap over indegrees), so legal
    chains of any length work regardless of Python's recursion limit —
    earlier ids are free to depend on later ids.
    """
    if roots is None:
        node_ids: Set[int] = set(by_id)
    else:
        node_ids = set()
        stack = list(roots)
        while stack:
            cur = stack.pop()
            if cur in node_ids:
                continue
            node_ids.add(cur)
            ref = by_id[cur].ref_id
            if by_id[cur].start_mode == "ref" and ref in by_id:
                stack.append(ref)

    indegree: Dict[int, int] = {}
    dependents: Dict[int, List[int]] = {}
    for i in node_ids:
        ref = by_id[i].ref_id
        if by_id[i].start_mode == "ref" and ref in node_ids:
            indegree[i] = indegree.get(i, 0) + 1
            dependents.setdefault(ref, []).append(i)
        else:
            indegree.setdefault(i, 0)

    heap: List[int] = [i for i, d in indegree.items() if d == 0]
    heapq.heapify(heap)
    order: List[int] = []
    while heap:
        cur = heapq.heappop(heap)
        order.append(cur)
        for nxt in dependents.get(cur, ()):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                heapq.heappush(heap, nxt)
    return order


def resolve_starts(
    items: List[Item], by_id: Dict[int, Item]
) -> Dict[int, int]:
    """Resolve every item id to its absolute start time, iteratively.

    Walked in the shared :func:`dependency_order` (ancestors first), so an
    item's referenced start/end is already known regardless of whether it
    points at a smaller or a larger id; no Python call stack grows with
    the chain length.
    """
    starts: Dict[int, int] = {}
    for x_id in dependency_order(by_id):
        x = by_id[x_id]
        if x.start_mode == "abs":
            value = x.start_ms or 0
        else:
            ref_start = starts[x.ref_id]
            base = ref_start if x.ref_edge == "start" \
                else ref_start + by_id[x.ref_id].duration
            value = base + (x.ref_offset or 0)
        starts[x_id] = value
    return starts


# --------------------------------------------------------------------------
# Full resolution: per-channel fade segments
# --------------------------------------------------------------------------

def resolve(items: Iterable[Item]) -> ResolvedView:
    items = [it for it in items if it.id is not None]
    validate(items)
    by_id = {it.id: it for it in items}
    starts = resolve_starts(items, by_id)

    grouped: Dict[Tuple[str, int], List[Item]] = {}
    for it in items:
        grouped.setdefault((it.fixture, it.channel), []).append(it)

    resolved: Dict[int, ResolvedItem] = {}
    lanes: Dict[str, List[Segment]] = {}
    for (fixture, channel), lane_items in grouped.items():
        lane_items.sort(key=lambda it: (starts[it.id], it.id))
        segs: List[Segment] = []
        value = Fraction(0)  # current unrounded channel value
        for pos, it in enumerate(lane_items):
            s = starts[it.id]
            e = s + it.duration
            # value of the prior fade at time s (the fade may be cut short)
            value = _value_at(segs, s, value)
            target = Fraction(it.target)
            if it.duration == 0:
                segs.append(Segment(s, s, target, target, True, it.id))
                value = target
                interrupted_by = None
            else:
                segs.append(Segment(s, e, value, target, False, it.id))
                value = target
                nxt = lane_items[pos + 1] if pos + 1 < len(lane_items) else None
                interrupted_by = nxt.id if nxt is not None and starts[nxt.id] < e \
                    else None
            resolved[it.id] = ResolvedItem(it, s, e, interrupted_by)
        lanes[f"{fixture}|{channel}"] = segs

    out = [resolved[it.id] for it in items]  # reference/input order
    return ResolvedView(items=out, lanes=lanes)


def _value_at(segs: List[Segment], t: int, fallback: Fraction) -> Fraction:
    for seg in reversed(segs):
        if seg.instant:
            if t >= seg.begin:
                return seg.end_value
            continue
        if t >= seg.end:
            return seg.end_value
        if t >= seg.begin:
            if seg.end == seg.begin:
                return seg.end_value
            frac = Fraction(t - seg.begin, seg.end - seg.begin)
            return seg.start_value + (seg.end_value - seg.start_value) * frac
    return fallback


# An evaluation instant.  Item starts/durations stay integer milliseconds,
# but the playhead may sit at any non-negative fractional millisecond; an
# int is accepted everywhere a Time is expected (Fraction arithmetic mixes).
Time = Fraction


# --------------------------------------------------------------------------
# Evaluation at an arbitrary absolute time
# --------------------------------------------------------------------------

def eval_lane(segs: List[Segment], t: Time) -> Tuple[Fraction, Optional[int]]:
    """Return (exact value, item id responsible for the value) at time t.

    Segments are start-ordered; an interrupted segment can still span t,
    so the *last* segment whose begin <= t wins.
    """
    value = Fraction(0)
    active: Optional[int] = None
    for seg in segs:
        if t < seg.begin:
            break
        if seg.instant:
            value = seg.end_value
            active = seg.item_id
            continue
        if t >= seg.end:
            value = seg.end_value
        else:
            frac = Fraction(t - seg.begin, seg.end - seg.begin)
            value = seg.start_value + (seg.end_value - seg.start_value) * frac
        active = seg.item_id
    return value, active


def evaluate(view: ResolvedView, t: Time) -> Dict[str, dict]:
    if t < 0:
        raise EngineError("时间不能为负")
    out: Dict[str, dict] = {}
    for key, segs in view.lanes.items():
        raw, active = eval_lane(segs, t)
        fixture, channel = key.split("|", 1)
        out[key] = {
            "fixture": fixture,
            "channel": int(channel),
            "raw": str(raw),
            "value": round_half_up(raw),
            "active_item_id": active,
        }
    return out


def round_half_up(x: Fraction) -> int:
    """Round to nearest integer; exact .5 goes up (away from zero)."""
    q, r = divmod(x.numerator, x.denominator)
    if r * 2 >= x.denominator:
        q += 1
    return q


# --------------------------------------------------------------------------
# Cross-project fragment porting
# --------------------------------------------------------------------------

def port_fragment(
    items: Iterable[Item], selected_ids: Iterable[int], landing_ms: int
) -> List[Tuple[int, Item]]:
    """Plan copying a fragment of ``items`` into another project.

    The fragment is the selected items plus *all* their upstream
    (transitively referenced) items, treated as one batch — a shared
    ancestor appears exactly once.  The fragment's earliest resolved
    actual start is aligned to ``landing_ms``; relative spacing, fade
    durations and reference start/end semantics are preserved.

    Returns ``(old_id, new_item)`` pairs in ancestor-first order so the
    caller can insert rows one by one and rewire ``ref_id`` to the freshly
    allocated id of an ancestor that is already written.  The returned
    items carry ``id=None`` and still hold the *old* ``ref_id`` values.

    Raises :class:`EngineError` on an empty selection, an unknown item id
    or a negative landing point.
    """
    items = [it for it in items if it.id is not None]
    by_id = {it.id: it for it in items}
    if landing_ms < 0:
        raise EngineError("落点必须是非负整数毫秒")
    selected = list(dict.fromkeys(selected_ids))  # dedupe, keep order
    if not selected:
        raise EngineError("未选择要移植的提示项")
    for sid in selected:
        if sid not in by_id:
            raise EngineError(f"提示项 #{sid} 在源工程中不存在")

    # selected items plus every upstream referenced item, as one batch
    closure = set()
    stack = list(selected)
    while stack:
        cur = stack.pop()
        if cur in closure:
            continue
        closure.add(cur)
        it = by_id[cur]
        if it.start_mode == "ref":
            if it.ref_id not in by_id:
                raise EngineError(f"提示项 {it.ref_id} 不存在，引用缺失")
            stack.append(it.ref_id)

    # actual starts come from resolving the source project's references
    starts = resolve_starts(items, by_id)
    earliest = min(starts[i] for i in closure)
    shift = landing_ms - earliest  # every actual start moves by this delta

    # ancestors first, so a reference is always rewired to an ancestor
    # whose new id is already allocated; the same iterative dependency
    # order used for resolving times (id order breaks ties, so a legal
    # chain of any length — including early ids pointing at later ids —
    # never hits Python's recursion limit).
    order = dependency_order(by_id, closure)

    plan: List[Tuple[int, Item]] = []
    for old_id in order:
        it = by_id[old_id]
        if it.start_mode == "abs":
            moved = Item(None, it.fixture, it.channel, it.target, it.duration,
                         "abs", start_ms=starts[it.id] + shift)
        else:
            moved = Item(None, it.fixture, it.channel, it.target, it.duration,
                         "ref", ref_id=it.ref_id, ref_edge=it.ref_edge,
                         ref_offset=it.ref_offset)
        plan.append((old_id, moved))
    return plan


# --------------------------------------------------------------------------
# Reference graph helpers (for the UI chain highlighting)
# --------------------------------------------------------------------------

def referenced_by_map(items: Iterable[Item]) -> Dict[int, set]:
    """item id -> set of ids that reference it directly."""
    refs: Dict[int, set] = {}
    for it in items:
        if it.start_mode == "ref" and it.ref_id is not None:
            refs.setdefault(it.ref_id, set()).add(it.id)
    return refs


def referrer_chain(items: Iterable[Item], target_id: int) -> List[int]:
    """Transitive reverse closure: items whose start (in)directly refs target."""
    result: List[int] = []
    seen = {target_id}
    frontier = [target_id]
    refs = referenced_by_map(items)
    while frontier:
        cur = frontier.pop()
        for nxt in sorted(refs.get(cur, ())):
            if nxt not in seen:
                seen.add(nxt)
                result.append(nxt)
                frontier.append(nxt)
    return result
