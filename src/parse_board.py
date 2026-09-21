"""
parse_board.py — turn a real KiCad board into nets and adjacencies.

This is the step that makes the graph real instead of invented. A .kicad_pcb
file stores every copper trace as a segment: two endpoints in millimeters, a
width, a layer, and the net it belongs to. None of that says "these two nets
run dangerously close." That relationship is not in the file. We compute it.

The flow:
  1. read every trace segment and group it by net,
  2. read each net's name and class (data / power / clock),
  3. for each pair of nets, look for segments that run parallel, on the same
     layer, close enough to matter, and estimate the gap, the parallel length,
     and the layer,
  4. emit one adjacency per qualifying pair, carrying those values.

The adjacency is the edge the whole engine reasons over. The fault lives in
the edge, so this file is where the fault first becomes visible.

Nothing here decides what is *dangerous*. It measures relationships and their
values. The danger rule and the severity score are applied later, in the
resolver, against the numbers this file produces. Keeping measurement separate
from judgment is deliberate: the graph states facts, the resolver interprets
them.

Two honest limits, stated once here:

  - The gap and the parallel length are ESTIMATES read off the routed geometry,
    not field-solver measurements. The gap is sampled at one point of the run
    (see _parallel_overlap) and the length is the shorter segment's length. Both
    are good proxies for a clean parallel run and looser for a tapering or
    offset one. We call them estimates in the tutorial, not measurements.

  - The pairing is brute force: every net pair, every segment pair, which is
    fine at this board's scale (tens of nets) and would need spatial indexing on
    a board of thousands. We scoped to the high-speed nets on purpose.

  - We parse straight (segment ...) tracks only, matching the KiCad v6/v7 board
    format. This board has no arcs; a board that routes high-speed lanes as arcs
    would need arc handling added.
"""

import re
import math
from dataclasses import dataclass

import config as C


# --- Data shapes ------------------------------------------------------------

@dataclass
class Net:
    """One net: a single electrical wire, a node in the graph."""
    key: str          # stable id, e.g. "net_20"
    net_id: int       # the KiCad net number
    name: str         # the full net name from the board
    net_class: str    # data / power / clock / ground / unknown
    activity: float = 1.0   # how active as an aggressor (0..1); set from config


@dataclass
class Adjacency:
    """One close parallel run between two nets: an edge in the graph.

    The values here are estimated off the geometry. spacing_mm is the
    edge-to-edge gap sampled at the closest parallel overlap, parallel_len_mm is
    how far the two run alongside each other, layer is the copper layer they
    share.
    """
    victim_key: str
    aggressor_key: str
    victim_name: str
    aggressor_name: str
    aggressor_class: str
    spacing_mm: float
    parallel_len_mm: float
    layer: str
    trace_width_mm: float   # the narrower of the two traces, for the 3W rule
    aggressor_activity: float = 1.0   # aggressor net activity (0..1)


# --- Segment parsing --------------------------------------------------------

# A KiCad track segment:
#   (segment (start X Y) (end X Y) (width W) (layer "L") [(locked yes)] (net N))
_SEG_RE = re.compile(
    r'\(segment\s+\(start ([\d.-]+) ([\d.-]+)\)\s+'
    r'\(end ([\d.-]+) ([\d.-]+)\)\s+'
    r'\(width ([\d.-]+)\)\s+'
    r'\(layer "([^"]+)"\)'
    r'(?:\s+\(locked \w+\))?\s+'
    r'\(net (\d+)\)'
)

_NET_RE = re.compile(r'\(net (\d+) "([^"]*)"\)')


@dataclass
class _Seg:
    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    layer: str


def _read_file(path: str) -> str:
    return open(path, encoding="utf-8", errors="replace").read()


def _parse_nets(txt: str) -> dict:
    """Map net id -> name from the board's net table."""
    return {int(m.group(1)): m.group(2) for m in _NET_RE.finditer(txt)}


def _parse_segments(txt: str, keep_ids: set) -> dict:
    """Map net id -> list of segments, for the nets we keep."""
    segs = {}
    for m in _SEG_RE.finditer(txt):
        x1, y1, x2, y2, w, layer, net = m.groups()
        net = int(net)
        if net not in keep_ids:
            continue
        segs.setdefault(net, []).append(
            _Seg(float(x1), float(y1), float(x2), float(y2), float(w), layer)
        )
    return segs


# --- Geometry ---------------------------------------------------------------

def _length(s: _Seg) -> float:
    return math.hypot(s.x2 - s.x1, s.y2 - s.y1)


def _angle_deg(s: _Seg) -> float:
    """Segment orientation, folded to 0..180 so direction does not matter."""
    return math.degrees(math.atan2(s.y2 - s.y1, s.x2 - s.x1)) % 180.0


def _midpoint(s: _Seg):
    return (s.x1 + s.x2) / 2.0, (s.y1 + s.y2) / 2.0


def _point_to_segment_mm(px: float, py: float, s: _Seg) -> float:
    """Shortest distance from a point to a segment, in board units (mm)."""
    ax, ay, bx, by = s.x1, s.y1, s.x2, s.y2
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _parallel_overlap(a: _Seg, b: _Seg):
    """If a and b run parallel on the same layer and close, return
    (gap_mm, overlap_mm); otherwise None.

    The gap is estimated from a's midpoint to b, a good proxy for the local
    spacing of a true parallel run and looser if the two fan apart. The overlap
    is the shorter of the two lengths, a proxy for how far they run together
    that can overstate an offset run. Both are estimates, not field-solver
    measurements; the tutorial says so.
    """
    if a.layer != b.layer:
        return None
    if _length(a) < C.MIN_PARALLEL_LEN_MM or _length(b) < C.MIN_PARALLEL_LEN_MM:
        return None
    da = abs(_angle_deg(a) - _angle_deg(b))
    da = min(da, 180.0 - da)
    if da > C.PARALLEL_ANGLE_TOL_DEG:
        return None
    mx, my = _midpoint(a)
    gap = _point_to_segment_mm(mx, my, b)
    if gap > C.MAX_GAP_MM:
        return None
    overlap = min(_length(a), _length(b))
    return gap, overlap


# --- Intended-routing filters ----------------------------------------------
# Two kinds of closeness are DESIGNED IN, not defects, and must be kept out of
# the graph or the engine's worst offender is always correct design:
#
#   1. A differential pair (CAM1_D0_P / CAM1_D0_N) is one signal carried on two
#      lines meant to run tight and parallel. _is_diff_partner skips it.
#
#   2. The lanes of ONE interface (all of CAM1's lanes, all of DSI0's lanes) are
#      routed together as a bus, intentionally close and parallel for their
#      whole length. _same_interface skips those pairs. Without this, the board's
#      top adjacencies are all "the camera bus is a camera bus" — 38 of 48 on
#      the tutorial board — and the real cross-interface fault is buried.
#
# What survives both filters is the honest fault field: nets from DIFFERENT
# interfaces (or a rail against a signal) that end up close and long by routing
# accident, not by design. On the tutorial board that leaves 10 candidates, led
# by a camera lane and a display lane sharing a layer for 48 mm.

# A net name carries its interface in its path and subsystem token, e.g.
# /CM4_HighSpeed/CAM1_D3_P -> interface "CM4_HighSpeed/CAM1". We strip the lane
# suffix (_D3, _C, _P/_N, _TX/_RX, _CLK) to fold every lane of one interface to
# the same key.
_LANE_SUFFIX_RE = re.compile(r'([A-Za-z0-9]+?)(_D\d|_C(?:_|$)|_CLK|_TX\d*|_RX\d*|_P$|_N$)')


def _interface_of(name: str) -> str:
    """Return an interface key for a net name, folding all lanes of one bus to
    the same value. Names without a recognizable lane suffix fall back to the
    leaf, so unrelated nets never collide into one interface."""
    parts = name.strip("/").split("/")
    leaf = parts[-1]
    m = _LANE_SUFFIX_RE.match(leaf)
    sub = m.group(1) if m else leaf
    prefix = parts[-2] if len(parts) >= 2 else ""
    return (prefix + "/" + sub) if prefix else sub


def _same_interface(a: Net, b: Net) -> bool:
    """True if both nets belong to the same interface/bus, so their closeness is
    intended routing rather than a fault."""
    ia, ib = _interface_of(a.name), _interface_of(b.name)
    return ia == ib and ia != ""


def _is_diff_partner(a: Net, b: Net) -> bool:
    """True if a and b are the two halves of one differential pair.

    A differential pair is *designed* to run close and parallel, so it must not
    enter the graph as a dangerous adjacency. We detect a pair by identical
    names apart from a trailing polarity marker, covering the common
    conventions: P/N, P/M, and +/-, with or without a separating _ or - before
    the marker. Skipping them keeps the engine from flagging correct design.
    """
    na, nb = a.name.upper(), b.name.upper()
    if na == nb:
        return False
    for pos, neg in (("P", "N"), ("P", "M"), ("+", "-")):
        for x, y in ((pos, neg), (neg, pos)):
            if na.endswith(x) and nb.endswith(y):
                if na[:-len(x)].rstrip("_-") == nb[:-len(y)].rstrip("_-"):
                    return True
    return False


def _orient(a: Net, b: Net):
    """Pick which net is the victim and which the aggressor for the edge label.

    A signal net is the victim we diagnose; a louder net is the aggressor. A
    power rail or a clock aggresses into a data signal, not the other way around
    for diagnosis, so the data net is the victim. Between two nets of the same
    kind the choice is symmetric, so we keep a stable order and let the
    traversal start from whichever net the engineer reports.

    Note: the edge is stored in both directions at ingest, and the resolver
    reads the aggressor's class from whichever node the traversal lands on, so
    orientation only sets the human-readable victim/aggressor labels — but a
    clock or rail read as the victim would print backwards, so we fix it here.
    """
    AGGRESSOR_CLASSES = ("power", "clock")
    if a.net_class in AGGRESSOR_CLASSES and b.net_class == "data":
        return b, a
    if b.net_class in AGGRESSOR_CLASSES and a.net_class == "data":
        return a, b
    return a, b


# --- Public API -------------------------------------------------------------

def parse_board(path: str, scope_classes=("data", "power", "clock")):
    """Parse a KiCad board into nets and adjacencies.

    scope_classes limits the graph to the net classes worth reasoning over. The
    tutorial scopes to data, power, and clock: the high-speed subsystems, their
    rails, and the clock lanes, where crosstalk is real. Widen it further and the
    same pipeline runs unchanged on a larger graph.

    Returns (nets, adjacencies): a list of Net and a list of Adjacency.
    """
    txt = _read_file(path)
    id_to_name = _parse_nets(txt)

    # Keep only nets whose class is in scope. classify_net reads the class from
    # the name, the same field the severity score later reads for loudness.
    nets = {}
    for net_id, name in id_to_name.items():
        if not name:
            continue
        cls = C.classify_net(name)
        if cls not in scope_classes:
            continue
        key = f"net_{net_id}"
        activity = C.ACTIVITY_OVERRIDES.get(
            key, C.ACTIVITY_BY_CLASS.get(cls, C.ACTIVITY_BY_CLASS["unknown"]))
        nets[net_id] = Net(
            key=key,
            net_id=net_id,
            name=name,
            net_class=cls,
            activity=activity,
        )

    segs = _parse_segments(txt, set(nets))

    # For every pair of in-scope nets, find their closest qualifying parallel
    # run and record one adjacency carrying its values. We keep the worst
    # (highest coupling) overlap per pair, since that is the one that would
    # drive a fault.
    adjacencies = []
    ids = list(nets)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a_id, b_id = ids[i], ids[j]
            a, b = nets[a_id], nets[b_id]

            # Skip closeness that is designed in, not a defect: a differential
            # pair's two halves, and any two lanes of the same interface/bus.
            if _is_diff_partner(a, b):
                continue
            if _same_interface(a, b):
                continue

            best = None  # (score, gap, overlap, layer, width)
            for sa in segs.get(a_id, []):
                for sb in segs.get(b_id, []):
                    res = _parallel_overlap(sa, sb)
                    if res is None:
                        continue
                    gap, overlap = res
                    width = min(sa.width, sb.width)
                    # rank overlaps within a pair by coupling proxy: long and
                    # close is worse. This only selects which overlap to keep,
                    # not the final severity, which the resolver computes.
                    score = overlap / (gap + 0.05)
                    if best is None or score > best[0]:
                        best = (score, gap, overlap, sa.layer, width)
            if best is None:
                continue
            _, gap, overlap, layer, width = best

            # Orient the edge label: the data net is the victim we diagnose, a
            # rail or clock is the aggressor. Between two data nets the choice is
            # symmetric; the resolver traverses from whichever net is named.
            victim, aggressor = _orient(a, b)
            adjacencies.append(Adjacency(
                victim_key=victim.key,
                aggressor_key=aggressor.key,
                victim_name=victim.name,
                aggressor_name=aggressor.name,
                aggressor_class=aggressor.net_class,
                spacing_mm=round(gap, 4),
                parallel_len_mm=round(overlap, 3),
                layer=layer,
                trace_width_mm=round(width, 4),
                aggressor_activity=aggressor.activity,
            ))

    return list(nets.values()), adjacencies


if __name__ == "__main__":
    # Quick self-check against the tutorial board.
    import sys
    board = sys.argv[1] if len(sys.argv) > 1 else "data/CM4IOUSB3.kicad_pcb"
    nets, adj = parse_board(board)
    from collections import Counter
    print(f"nets in scope: {len(nets)}")
    print(f"class breakdown: {dict(Counter(n.net_class for n in nets))}")
    print(f"adjacencies:   {len(adj)}")
    adj.sort(key=lambda a: a.parallel_len_mm, reverse=True)
    print("\ntop adjacencies by parallel length:")
    for a in adj[:8]:
        print(f"  {a.victim_name[:28]:30s} <- {a.aggressor_name[:28]:30s} "
              f"[{a.aggressor_class:5s}] gap {a.spacing_mm}mm  parallel {a.parallel_len_mm}mm  {a.layer}")