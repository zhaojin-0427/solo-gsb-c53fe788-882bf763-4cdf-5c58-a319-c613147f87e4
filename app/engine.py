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

from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, Iterable, List, Optional, Tuple


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
        s = starts[it]
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
    node_ids: Iterable[int], by_id: Dict[int, Item]
) -> List[int]:
    """Return the given item ids (plus every item they reference, transitively)
    in ancestor-first (reference-topological) order: every referenced item is
    emitted before the item referencing it.

    This is the single dependency ordering shared by start-time resolution
    and cross-project fragment porting — times are computed in it and ported
    rows are written in it, so an ancestor's resolved time / freshly
    allocated id is always available when its dependents need it.

    Roots are visited in the given order, making the result deterministic
    for a fixed input.  The walk is deliberately iterative: a legal chain
    may be hundreds of levels long (earlier ids may legitimately reference
    later ids), and a recursive walk would exhaust Python's recursion
    limit.  ``validate`` has already rejected missing references and cycles
    before this runs; the guards here only turn such a graph into an
    :class:`EngineError` instead of an infinite loop / ``KeyError``.
    """
    roots = list(dict.fromkeys(node_ids))  # dedupe, keep first occurrence
    order: List[int] = []
    emitted: set = set()

    for root in roots:
        if root in emitted:
            continue
        # each frame: (item id, whether its ancestor has already been entered)
        stack: List[Tuple[int, bool]] = [(root, False)]
        gray: set = set()
        while stack:
            i, entered = stack[-1]
            if entered:
                stack.pop()
                gray.discard(i)
                if i not in emitted:
                    emitted.add(i)
                    order.append(i)
                continue
            it = by_id.get(i)
            if it is None:
                raise EngineError(f"提示项 {i} 不存在，引用缺失")
            stack[-1] = (i, True)
            gray.add(i)
            if it.start_mode == "ref":
                ref = it.ref_id
                if ref in gray:
                    chain = [n for n, _ in stack]
                    if ref in chain:
                        chain = chain[chain.index(ref):]
                    path = " -> ".join("#" + str(n) for n in chain + [ref])
                    raise EngineError(f"引用链存在环：{path}")
                if ref not in emitted:
                    if ref not in by_id:
                        raise EngineError(f"提示项 {ref} 不存在，引用缺失")
                    stack.append((ref, False))
    return order


def resolve_starts(
    items: List[Item], by_id: Dict[int, Item]
) -> Dict[Item, int]:
    """Resolve every item to its absolute start time.

    Items are processed in the iterative :func:`dependency_order`, so a
    legal reference chain of arbitrary length — including one where earlier
    ids depend on later ids — is resolved without recursion.  Both start
    and end references are served from the same already-computed table, so
    edge/offset arithmetic is identical to the old recursive walk.
    """
    starts: Dict[int, int] = {}
    for i in dependency_order(
        (it.id for it in items if it.id is not None), by_id
    ):
        it = by_id[i]
        if it.start_mode == "abs":
            starts[i] = it.start_ms or 0
        else:
            ref_start = starts[it.ref_id]
            base = ref_start if it.ref_edge == "start" \
                else ref_start + by_id[it.ref_id].duration
            starts[i] = base + (it.ref_offset or 0)

    resolved: Dict[Item, int] = {}
    for it in items:
        if it.id is not None:
            resolved[it] = starts[it.id]
        elif it.start_mode == "abs":
            # anonymous items are never referenced themselves
            resolved[it] = it.start_ms or 0
        else:
            ref_start = starts[it.ref_id]
            base = ref_start if it.ref_edge == "start" \
                else ref_start + by_id[it.ref_id].duration
            resolved[it] = base + (it.ref_offset or 0)
    return resolved


# --------------------------------------------------------------------------
# Full resolution: per-channel fade segments
# --------------------------------------------------------------------------

def resolve(items: Iterable[Item]) -> ResolvedView:
    items = [it for it in items if it.id is not None]
    validate(items)
    by_id = {it.id: it for it in items}
    starts = resolve_starts(items, by_id)

    ordered = sorted(items, key=lambda it: (starts[it], it.id))
    resolved: Dict[int, ResolvedItem] = {}

    grouped: Dict[Tuple[str, int], List[Item]] = {}
    for it in ordered:
        grouped.setdefault((it.fixture, it.channel), []).append(it)

    lanes: Dict[str, List[Segment]] = {}
    for (fixture, channel), lane_items in grouped.items():
        lane_items.sort(key=lambda it: (starts[it], it.id))
        segs: List[Segment] = []
        value = Fraction(0)  # current unrounded channel value
        for k, it in enumerate(lane_items):
            s = starts[it]
            e = s + it.duration
            # Value of the prior fade at time s.  Segments are appended in
            # start order and same-(fixture,channel,instant) pairs were
            # rejected above, so the last segment is the unique one with the
            # greatest begin < s: sample it directly instead of rescanning
            # every prior segment (which made long lanes quadratic).
            if segs:
                prev = segs[-1]
                if prev.instant or s >= prev.end:
                    value = prev.end_value
                else:
                    # a non-instant segment has end == begin + duration > begin
                    frac = Fraction(s - prev.begin, prev.end - prev.begin)
                    value = prev.start_value + \
                        (prev.end_value - prev.start_value) * frac
            target = Fraction(it.target)
            interrupted_by = None
            if it.duration == 0:
                segs.append(Segment(s, s, target, target, True, it.id))
                value = target
            else:
                segs.append(Segment(s, e, value, target, False, it.id))
                value = target
                # lane_items is start-sorted; the next item is the next
                # element (same-instant starts on this lane cannot exist)
                nxt = lane_items[k + 1] if k + 1 < len(lane_items) else None
                if nxt is not None and starts[nxt] < e:
                    interrupted_by = nxt.id
            resolved[it.id] = ResolvedItem(it, s, e, interrupted_by)
        lanes[f"{fixture}|{channel}"] = segs

    out = [resolved[it.id] for it in items]  # reference/input order
    return ResolvedView(items=out, lanes=lanes)


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
    earliest = min(starts[by_id[i]] for i in closure)
    shift = landing_ms - earliest  # every actual start moves by this delta

    # ancestors first, so a reference is always rewired to an ancestor
    # whose new id is already allocated; deterministic for a given source.
    # This is the same iterative dependency order that resolved the starts
    # above, so long legal chains (including earlier -> later ids) port
    # without recursion.
    order = dependency_order(sorted(closure), by_id)

    plan: List[Tuple[int, Item]] = []
    for old_id in order:
        it = by_id[old_id]
        if it.start_mode == "abs":
            moved = Item(None, it.fixture, it.channel, it.target, it.duration,
                         "abs", start_ms=starts[it] + shift)
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
