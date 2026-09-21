"""
config.py — every knob the crosstalk engine turns, in one place.

The rest of the pipeline reads from here. Change a value in this file and the
whole engine follows: the net classes it recognizes, the design rule that
decides when an adjacency is dangerous, and the weights that rank one dangerous
adjacency above another. Nothing downstream hard-codes these numbers.

Keeping the policy in config, not in code, is a deliberate choice. A reader
who wants to point this at their own board swaps their process design kit (PDK)
rule in here and reruns, without touching a line of query or resolver logic.
"""

# --- Database ---------------------------------------------------------------
# One board's graph lives in one database. The name carries a version so a
# rebuild never clobbers an earlier run.
DB_NAME = "crosstalk_v1"

GRAPH_NAME = "board_topology"          # the named graph the traversal walks
NETS = "nets"                          # document collection: nets (nodes)
ADJACENCIES = "adjacencies"            # edge collection: close parallel pairs
DEFECTS = "defect_reports"             # vector collection: past cases

# --- Models -----------------------------------------------------------------
# Ingestion and query must share one embedding model. A dimension mismatch
# breaks vector search, so this value is read in both places, never retyped.
EMBEDDING_MODEL = "text-embedding-3-small"   # 1536 dims
CHAT_MODEL = "gpt-5-mini"                    # the judge and Ada run on this

# --- Net classification -----------------------------------------------------
# A net's class decides how loud it is as an aggressor. We read the class from
# the net name, which on a real board carries the subsystem (USB3, PCIe, CAM,
# DSI) and the power rails (+12v, +5v, 3.3v). The tutorial board (Raspberry Pi
# CM4 IO) names its nets this way, and most boards do.
#
# These are substring matches against the upper-cased net name.
DATA_KEYS = ["PCIE", "USB3", "PEXTX", "HDMI_D", "CAM0", "CAM1", "DSI0", "DSI1"]
POWER_KEYS = ["+12V", "+5V", "+3.3V", "3V3", "A3.3", "VCC", "VDD", "HDMI_5V"]
GROUND_KEYS = ["GND"]
# Clock lanes are the loudest aggressors: they toggle continuously at speed.
# On this board they are the MIPI D-PHY clock lanes (…_C_N / …_C_P) and the
# PCIe reference clock (PCIE_CLK…). We match the clock pattern explicitly so a
# clock lane is never miscounted as a plain data lane.
CLOCK_KEYS = ["CLK", "CLOCK"]
CLOCK_SUFFIXES = ["_C_N", "_C_P", "_C"]   # MIPI D-PHY clock naming

# --- The design rule: when is an adjacency dangerous? -----------------------
# We do not invent a spacing threshold. We take the 3W rule, a standard
# signal-integrity guideline for parallel high-speed traces. Stated in full:
# keep center-to-center spacing at least three trace widths apart, which is the
# same as keeping edge-to-edge spacing at least two trace widths. Honoring it
# is credited with removing roughly 70% of crosstalk. IPC-2221 gives the
# related minimum-clearance floor.
#
# Our parser measures the edge-to-edge gap between traces, so we express the
# rule the same way: the danger line is an edge-to-edge gap below two trace
# widths. Below that, coupling climbs. We keep the multiple as a knob so you
# can tighten it (5W center-to-center for critical nets) or swap in your PDK's
# own rule and rerun, with no other edit.
#
# Source: 3W rule as documented across signal-integrity practice; IPC-2221 for
# the clearance floor. Verify the exact figures against your standard of record
# before publishing.
SPACING_RULE_EDGE_MULTIPLE = 2.0    # edge-to-edge danger line, in trace widths (the 2W in the 3W rule)

# An adjacency only counts as a candidate if the two traces actually run
# alongside each other, not just cross at a point. These two gates keep the
# graph honest: a real parallel run, over a real length.
PARALLEL_ANGLE_TOL_DEG = 20.0  # within this angle, treat two segments as parallel
MIN_PARALLEL_LEN_MM = 2.0      # ignore overlaps shorter than this

# The widest gap we bother recording as an edge at all. Anything past this is
# not a coupling concern on this board, so it never enters the graph. This is a
# recording cutoff, not the danger rule; the danger rule is SPACING_RULE above.
# The two are intentionally different: we record out to MAX_GAP_MM so the graph
# holds the near-misses too, then let the severity score rank them down. If the
# recording cutoff were the danger line, the graph would only ever contain
# dangerous edges and the ranking would have nothing to rank against.
MAX_GAP_MM = 1.0

# --- The severity score: ranking one danger above another -------------------
# Three things make an adjacency dangerous: how close the traces run (spacing),
# how long they run alongside (parallel length), and how strong the aggressor
# is (its net class, gated by whether it is actually switching). The score turns
# "close, long, loud-and-active" into one comparable number so the resolver can
# rank candidates.
#
# The exact formula, computed in agent.severity():
#
#     severity =  w_spacing  * spacing_norm
#              +  w_length   * length_norm
#              +  w_loudness * aggressor_strength
#
#   spacing_norm      : 1 at or inside the design rule, fading to 0 at twice it
#                       (agent._normalize_spacing).
#   length_norm       : parallel_len / LENGTH_REF_MM, capped at 1
#                       (agent._normalize_length).
#   aggressor_strength: loudness * activity for the aggressor's class
#                       (config.aggressor_strength, see the activity block).
#
# The weights are visible and adjustable on purpose. A reader who cares more
# about parallel length than absolute spacing changes the numbers here and the
# ranking shifts, with no other edit. They sum to 1.0 for readability; note
# that because aggressor_strength maxes out below 1 (the loudest class is 0.8),
# the loudness term never reaches its full weight, which is fine — severity is a
# comparison between suspects, not an absolute grade.
SEVERITY_WEIGHTS = {
    "spacing": 0.40,   # closer is worse
    "length": 0.40,    # longer parallel run is worse; equal to spacing
    "loudness": 0.20,  # a louder, actively switching aggressor is worse
}
# Why spacing and length are weighted equally. Crosstalk coupling grows with the
# parallel-run LENGTH as much as with closeness — energy accumulates along the
# coupled span. A spacing-dominant weighting (e.g. 0.45/0.35) lets a short, very
# tight brush-past outrank a long, tight parallel run, which is backwards for
# this fault: on the tutorial board an 8.5 mm run at 0.28 mm would beat the real
# 43 mm coupled run at 0.42 mm. Equal weight keeps "close" and "long" on the same
# footing, which matches the physics and puts the genuinely dangerous long run on
# top. Tune to your own boards; this is a policy knob, not a law.

# The parallel-run length that counts as "fully long" for the length term. A
# run at or beyond this scores 1 on length; shorter runs scale down. We set it
# near the longest real run on the tutorial board (about 58 to 62 mm) so the
# term spans the board's actual range. Change it to match your board.
LENGTH_REF_MM = 60.0

# --- Rank labels: turn the severity number into a readable relative tag ------
# The severity score ranks candidates for one victim; it is a comparison, not
# an absolute risk grade. So the label is RELATIVE to the top candidate: the
# strongest suspect is "leading", ones within LEADING_MARGIN of it are
# "plausible", and the rest are "unlikely". This is honest about what the score
# is (a ranking) and avoids implying false precision from a synthetic number.
RANK_LABELS = {
    "leading": "leading",       # the top candidate
    "plausible": "plausible",   # within LEADING_MARGIN of the top score
    "unlikely": "unlikely",     # further below
}
# NOTE: LEADING_MARGIN (here) and DECISIVE_SEVERITY_GAP (below) are independent
# knobs that answer different questions — "is this suspect close to the leader?"
# versus "does the leader decisively beat the field?" — and only happen to share
# a starting value. Tune them separately.
LEADING_MARGIN = 0.12   # a candidate within this of the top score is "plausible"

# --- Confidence tiers: how sure is the whole finding? ------------------------
# This is the tier of the FINDING as a whole, separate from the per-candidate
# rank label (which candidate) above. It is DERIVED from evidence in
# agent.derive_tier(), not read straight off the judge's self-reported
# confidence. The reason is honesty: a large-language-model asked for a
# high/medium/low confidence will swing between runs on identical data, and a
# clean geometry tempts it to say "high", which then reads as "confirmed" on a
# finding nothing has actually verified.
#
# So we separate two things the single word "confidence" was conflating:
#   - the geometry, which the graph PROVES and is stable, and
#   - the hypothesis status, which stays unproven until hardware confirms it.
#
# The tier names, in order of strength:
TIER_CONFIRMED    = "confirmed"      # ONLY with real hardware/log verification
TIER_CORROBORATED = "corroborated"   # decisive geometry + an independent citation that agrees
TIER_CANDIDATE    = "candidate"      # plausible, needs on-hardware confirmation
TIER_EXCLUDED     = "excluded"       # no adjacency explains it -> not crosstalk

# The gate that keeps us honest. The tutorial never measures a board, so this
# stays False and "confirmed" is structurally unreachable: the ceiling is
# "corroborated", no matter how confident the judge sounds. Flip this to True
# only in a deployment that actually feeds back a hardware or log confirmation.
HARDWARE_VERIFIED = False

# How decisively must the leading candidate beat the runner-up for the geometry
# to count as "decisive"? A wide severity gap is graph-proven and stable; a
# narrow one means several suspects are in play and the finding stays a plain
# candidate. This is the graph-side signal the derived tier leans on instead of
# the judge's mood. Independent of LEADING_MARGIN above (see that note).
DECISIVE_SEVERITY_GAP = 0.12

# How loud each aggressor class is, normalized 0..1. A power rail swinging
# current couples harder than a quiet signal; a high-speed data line with fast
# edges is louder than a static one. These are illustrative and live here to be
# tuned against a real board's behavior. Loudness is only half the story — see
# activity below, and aggressor_strength() for how the two combine.
AGGRESSOR_LOUDNESS = {
    "clock": 0.8,    # continuous fast toggling: the loudest aggressor
    "power": 0.7,    # a switching rail couples hard, but not every cycle
    "data": 0.55,    # a data lane is only as loud as its traffic
    "ground": 0.1,   # a quiet reference
    "unknown": 0.4,
}

# --- Activity status: is the aggressor actually switching? -------------------
# A net only couples when it is driven. Loudness says how hard a class *can*
# aggress; activity says whether this particular net is actually switching in
# the failing scenario. An idle aggressor is a weak suspect even if its geometry
# is bad, so the severity score gates loudness by activity (see
# aggressor_strength): the loudness term is loudness * activity, not loudness
# alone. A clock is effectively always on; a data lane depends on traffic; a
# ground reference does not aggress at all.
#
# On a real board this signal comes from the monitoring/telemetry layer or a
# functional test. Here it is a synthetic, per-class default we flag as
# illustrative. In production, replace ACTIVITY_BY_CLASS with a real per-net
# activity signal keyed by net id, or fill ACTIVITY_OVERRIDES per net.
ACTIVITY_BY_CLASS = {
    "clock": 1.0,    # a clock toggles continuously: always active
    "power": 0.9,    # a rail is live but its switching is bounded
    "data": 0.7,     # a data lane is active only with traffic
    "ground": 0.0,   # a quiet reference does not aggress
    "unknown": 0.6,
}

# Per-net overrides, keyed by net key (e.g. "net_36": 0.2 for a lane known
# idle in the failing scenario). Empty by default; the class default applies.
# This is the knob that lets you say "the loud clock next door was powered down
# during the failing test, so it is not the suspect the geometry alone suggests."
ACTIVITY_OVERRIDES = {}


def classify_net(name: str) -> str:
    """Return the class of a net from its name: data, power, ground, or unknown.

    The class is read from the name because that is where a real board records
    intent. It feeds two things: which nets can be victims or aggressors, and
    how loud an aggressor is in the severity score.
    """
    n = name.upper()
    # Clock check runs FIRST: a MIPI clock lane (…_C_N) also contains its
    # subsystem key (DSI0, CAM1) which is in DATA_KEYS, so without this it
    # would be misread as data. A clock is the loudest aggressor, so getting
    # this right sharpens the ranking.
    if any(k in n for k in CLOCK_KEYS) or any(n.endswith(s) for s in CLOCK_SUFFIXES):
        return "clock"
    if any(k in n for k in POWER_KEYS):
        return "power"
    if any(k in n for k in DATA_KEYS):
        return "data"
    if any(k in n for k in GROUND_KEYS):
        return "ground"
    return "unknown"


def aggressor_strength(net_class: str, activity: float = None) -> float:
    """Return the aggressor's effective strength: how loud its class is, gated
    by how active it is right now.

    Loudness is a property of the class — a clock toggles harder than a data
    lane. Activity is whether this particular net is actually switching in the
    failing scenario. A loud net sitting idle is a weak suspect, so the two
    MULTIPLY rather than add: an idle aggressor (activity 0) contributes nothing
    no matter how loud its class, and a fully active clock contributes its full
    loudness. Both are normalized 0..1, so the product is 0..1 and drops
    straight into the loudness term of the severity score.

    We keep this combination here, in config, rather than in the resolver, so
    all of the ranking policy — the classes, the weights, and how loudness and
    activity combine — lives in one file the reader can read and change.

    If activity is not supplied (None), fall back to the class default, so a net
    missing an explicit activity value never breaks the score.
    """
    loud = AGGRESSOR_LOUDNESS.get(net_class, AGGRESSOR_LOUDNESS["unknown"])
    if activity is None:
        activity = ACTIVITY_BY_CLASS.get(net_class, ACTIVITY_BY_CLASS["unknown"])
    return loud * activity