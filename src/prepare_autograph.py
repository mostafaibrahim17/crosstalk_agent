"""
prepare_autograph.py — write the synthetic defect corpus for AutoGraph.

The board geometry is real. The defect history is not, because no public
dataset ties past crosstalk incidents to trace geometry. So we write the
corpus, and we write it honestly: every case models a documented
signal-integrity mechanism (the 3W spacing guideline, parallel-run coupling,
guard traces, series termination, return paths, near-end and far-end
crosstalk), only the incident is invented. The tutorial says so in plain text.

Two things this file is careful about, because both would corrupt the result:

  1. Format. AutoGraph builds a knowledge graph by extracting entities and
     relationships from unstructured prose. So each report is written as a
     markdown document with a title and an engineer-facing body, the same
     shape the reference tutorials used. We do not hand AutoGraph structured
     JSON.

  2. No answer leakage. Our seeding labels (close_match / near_miss / filler)
     are bookkeeping for validating recall, not facts about the boards. They
     must never enter the documents AutoGraph reads, or the knowledge graph
     would learn our labels and the Retriever could cite them. The labels live
     only in a separate manifest, next to the corpus, never inside it.

Corpus shape (30 cases, seeded with intent):
  close_match (6): the hero's symptom and mechanism — far/near-end crosstalk
                   from a long, tight, same-layer parallel run. The mix matches
                   the board's real worst fault, a clock lane coupling into a
                   data lane: three clock-to-data cases and three data-to-data
                   cases, each with its own numbers from its own board and a
                   varied fix (widen spacing / guard trace / reroute to another
                   layer / shorten overlap / add a shield / ground stitching).
                   Recall surfaces these; the cited fix comes from them.
  near_miss  (12): the SAME surface symptom (frequency variation / jitter /
                   rising BER / eye closure) but a different, non-adjacency root
                   cause (impedance, return path, via stub, plane void, supply
                   ripple, ISI, reflections, skew, connector, EMI, termination,
                   ground bounce, dielectric loss). Spacing or reroute would not
                   fix them, so the judge has to earn its choice.
  filler     (12): unrelated board issues with a different symptom, so recall
                   ranks against a realistic field instead of a stacked deck.

Output:
  data/autograph/*.md          the documents AutoGraph ingests (no labels)
  data/autograph/manifest.json  id -> {file, role, title}, for our use only
"""

import os
import json


CASES = [
    # ================= close matches (6): hero symptom + mechanism ============
    # Mix: 3 clock-to-data (the board's real worst fault) + 3 data-to-data,
    # each with a distinct fix so the cited remediation varies by case.
    {
        "id": "dr_0001",
        "title": "Camera lane jitter from an adjacent high-speed signal",
        "role": "close_match",
        "body": (
            "During bring-up of a camera interface board, one high-speed lane "
            "showed frequency variation and a rising bit error rate. Every trace "
            "passed its own width and impedance check. The victim lane ran "
            "alongside a high-speed signal from a different interface for about "
            "45 mm on the bottom copper layer, with an edge-to-edge gap of "
            "roughly 0.3 mm, tighter than the two-trace-width separation the "
            "team's routing guideline calls for. The mechanism was far-end "
            "crosstalk: the aggressor's fast switching edges coupled into the "
            "victim over the long parallel run. The fix widened the separation "
            "to meet the guideline over the coupled span and shortened the "
            "overlap by moving one lane to an inner layer. The bit error rate "
            "returned to specification."
        ),
    },
    {
        "id": "dr_0002",
        "title": "Guard trace tames clock-to-data coupling on a shared layer",
        "role": "close_match",
        "body": (
            "A high-speed data lane developed timing jitter and eye-diagram "
            "closure where a clock lane from a different interface ran beside it. "
            "The two were unrelated nets that simply shared the bottom copper "
            "layer for a long span. Because a clock switches continuously with "
            "fast edges, it was a strong aggressor, and the behaviour matched "
            "near-end crosstalk over the run. The parallel overlap was close to "
            "44 mm at an edge-to-edge gap around 0.4 mm, inside the two-trace-"
            "width separation the routing guideline calls for. Board area did not "
            "allow a full reroute, so the fix added a grounded guard trace with "
            "stitching vias between the clock and the data lane and opened the "
            "spacing where it could. The jitter dropped and the eye reopened."
        ),
    },
    {
        "id": "dr_0003",
        "title": "Display lane errors from a long parallel run, fixed by reroute",
        "role": "close_match",
        "body": (
            "A display serial lane showed frequency variation and a climbing bit "
            "error rate. It shared a layer with an unrelated high-speed signal, "
            "and the two ran parallel for roughly 52 mm at a gap near 0.35 mm, "
            "inside the separation the design guideline recommends for the trace "
            "width in use. The coupling was far-end crosstalk over the long "
            "adjacent run. Board area near the victim was too tight for a guard "
            "trace, so the fix moved the aggressor to a different layer with an "
            "intact reference plane between the two, cutting the parallel overlap "
            "to almost nothing. The errors cleared."
        ),
    },
    {
        "id": "dr_0004",
        "title": "Clock-to-data coupling over a long adjacent run",
        "role": "close_match",
        "body": (
            "A data lane developed timing jitter and a rising bit error rate on a "
            "board where a clock lane ran beside it for a long span. The two "
            "shared the bottom copper layer for about 56 mm at an edge-to-edge "
            "gap near 0.4 mm. Because a clock switches continuously with fast "
            "edges, it was a strong aggressor even at that spacing, and the "
            "coupling read as far-end crosstalk that shifted the victim's edges. "
            "The fix rerouted the clock onto an inner layer with a solid "
            "reference plane between the two and added ground stitching, removing "
            "the long adjacency. The jitter and the errors cleared."
        ),
    },
    {
        "id": "dr_0005",
        "title": "Bit errors cleared by shortening a coupled bus segment",
        "role": "close_match",
        "body": (
            "Two parallel high-speed nets on the same outer layer ran together "
            "for roughly 60 mm at a gap around 0.33 mm and produced frequency "
            "variation and rising BER on the quieter of the two. The mechanism "
            "was far-end crosstalk accumulated over the long coupled length. "
            "Rather than widen the whole run, the team broke the parallelism: "
            "they split the coupled segment with a short jog to another layer so "
            "the two nets no longer ran alongside for more than a few "
            "millimetres. Reducing the coupled length below the far-end "
            "threshold returned the lane to specification."
        ),
    },
    {
        "id": "dr_0006",
        "title": "Clock coupling into a serial lane fixed by widening and shielding",
        "role": "close_match",
        "body": (
            "A serial data lane showed frequency variation and occasional bit "
            "errors traced to a neighbouring clock lane that ran parallel for "
            "about 47 mm at roughly 0.36 mm spacing on a shared layer. Because a "
            "clock switches continuously, it coupled strongly even at that "
            "spacing, and the mechanism read as far-end crosstalk over the long "
            "run. Board area allowed a local spacing increase, so the fix pulled "
            "the clock and the lane apart to beyond the two-trace-width guideline "
            "across the coupled span and added a grounded shield where the "
            "pull-apart was not enough. Link margin returned and the errors "
            "cleared."
        ),
    },

    # ================= near misses (12): hero symptom, other cause ============
    {
        "id": "dr_0007",
        "title": "Frequency variation traced to an impedance mismatch",
        "role": "near_miss",
        "body": (
            "A data lane showed frequency variation and an elevated bit error "
            "rate that first looked like crosstalk. Investigation found no "
            "aggressor running close and parallel. The real cause was an "
            "impedance discontinuity: a trace-width change at a connector "
            "transition reflected energy back down the line. The fix corrected "
            "the trace width for controlled impedance. Spacing to neighbours was "
            "already within the routing guideline, so opening it up would not "
            "have helped."
        ),
    },
    {
        "id": "dr_0008",
        "title": "Rising bit error rate from a ground-plane void",
        "role": "near_miss",
        "body": (
            "A high-speed net degraded with a rising bit error rate and jitter. "
            "The symptom resembled coupling, but the neighbouring traces were "
            "well separated. The cause was a void in the reference plane beneath "
            "the trace that severed the low-inductance return path and forced the "
            "return current to spread, raising effective inductance and slowing "
            "the rise time. The fix filled the plane void and routed the return "
            "path continuously under the signal. Trace-to-trace spacing was not "
            "the problem."
        ),
    },
    {
        "id": "dr_0009",
        "title": "Eye closure from a via stub, not from coupling",
        "role": "near_miss",
        "body": (
            "A serial lane showed eye-diagram closure and a rising bit error rate "
            "that looked like inter-lane crosstalk. No close parallel aggressor "
            "was present. The cause was a via stub left by a layer transition; at "
            "the lane's data rate the stub reflected and radiated, distorting the "
            "waveform. The fix back-drilled the stub and reduced the number of "
            "layer changes. Opening the spacing to neighbours would not have "
            "changed the eye."
        ),
    },
    {
        "id": "dr_0010",
        "title": "Jitter from a return-path discontinuity at a layer change",
        "role": "near_miss",
        "body": (
            "A high-speed signal showed frequency variation and jitter after a "
            "layer transition. It read like coupling, but the adjacent nets were "
            "spaced within the guideline. The real cause was a return-path "
            "discontinuity: the signal changed reference planes with no stitching "
            "via nearby, so the return current detoured and raised loop "
            "inductance. The fix added a ground stitching via at the transition "
            "to give the return current a continuous path. Spacing was fine."
        ),
    },
    {
        "id": "dr_0011",
        "title": "Clock coupling fixed by edge-rate control, not by spacing",
        "role": "near_miss",
        "body": (
            "A data line showed jitter and occasional bit errors where a clock "
            "ran nearby. Coupling was present, but the dominant fault was the "
            "clock's fast edge rate rather than a long parallel run: the two "
            "shared only a short adjacency, well under the length that drives "
            "far-end crosstalk. The fix added a series termination resistor on "
            "the clock to slow the edge and made a small timing-skew adjustment, "
            "which cleared the errors. Opening the spacing over the short overlap "
            "changed little. This is an edge-rate and timing fix, not the "
            "spacing-driven crosstalk seen on long high-speed parallel runs."
        ),
    },
    {
        "id": "dr_0012",
        "title": "Bit errors from power-supply ripple on a driver rail",
        "role": "near_miss",
        "body": (
            "A high-speed lane showed a rising bit error rate that suggested "
            "trace coupling. The neighbours were well separated. The cause was "
            "supply noise: a switching regulator put ripple onto the rail feeding "
            "the lane's driver, and that ripple modulated the driver's output. "
            "The fix added decoupling at the driver and cleaned up the "
            "regulator's feedback layout. No trace-to-trace adjacency was "
            "involved, so a spacing or reroute change would not have helped."
        ),
    },
    {
        "id": "dr_0013",
        "title": "Intersymbol interference from an under-equalized channel",
        "role": "near_miss",
        "body": (
            "A long backplane lane showed a closing eye and rising bit error rate "
            "that looked like coupling from a neighbour. Neighbours were spaced "
            "within the guideline. The real cause was intersymbol interference: "
            "the channel's loss at the signalling rate smeared symbols into one "
            "another. The fix enabled transmit de-emphasis and receiver "
            "equalization for the channel. Widening trace spacing would not have "
            "addressed a loss-driven ISI problem."
        ),
    },
    {
        "id": "dr_0014",
        "title": "Reflections from an unterminated stub branch",
        "role": "near_miss",
        "body": (
            "A signal showed frequency-dependent variation and errors that "
            "resembled crosstalk. No long parallel aggressor existed. The cause "
            "was a T-branch left unterminated: the stub reflected energy back "
            "onto the main line at certain frequencies. The fix removed the "
            "branch and routed point-to-point with a proper termination. A "
            "spacing change would not have removed the reflection."
        ),
    },
    {
        "id": "dr_0015",
        "title": "Skew-induced errors on a mismatched-length pair",
        "role": "near_miss",
        "body": (
            "A differential lane showed jitter and rising BER that first looked "
            "like an external aggressor. The pair's two halves were length "
            "mismatched, so intra-pair skew converted to common-mode noise and "
            "degraded the eye. The fix added length-matching serpentines to "
            "equalize the pair. Neighbour spacing was within the guideline and "
            "was not the cause."
        ),
    },
    {
        "id": "dr_0016",
        "title": "Connector footprint discontinuity mimicking coupling",
        "role": "near_miss",
        "body": (
            "A high-speed lane crossing a board-to-board connector showed eye "
            "closure and errors. It looked like coupling from the dense "
            "neighbourhood at the connector, but the neighbours were spaced "
            "correctly. The real cause was an impedance discontinuity in the "
            "connector footprint's anti-pad geometry. The fix tuned the anti-pad "
            "and ground-via arrangement for controlled impedance. Spacing was "
            "not the issue."
        ),
    },
    {
        "id": "dr_0017",
        "title": "EMI ingress from an external source, not a neighbour trace",
        "role": "near_miss",
        "body": (
            "A lane intermittently showed frequency variation and errors that "
            "correlated with an external switching event, not with any on-board "
            "neighbour. The cause was radiated EMI ingress through an unshielded "
            "section, not trace-to-trace coupling. The fix added local shielding "
            "and improved the ground return in that section. No adjacency change "
            "would have helped."
        ),
    },
    {
        "id": "dr_0018",
        "title": "Crosstalk-like errors traced to a noisy ground bounce",
        "role": "near_miss",
        "body": (
            "Several lanes near a large digital block showed correlated jitter "
            "and errors that looked like mutual coupling. The neighbours were "
            "within the spacing guideline. The cause was ground bounce from the "
            "digital block's simultaneous switching, shifting the local reference "
            "for the nearby lanes. The fix improved the block's power delivery "
            "and ground stitching. Rerouting individual lanes apart would not "
            "have addressed a shared-reference problem."
        ),
    },

    # ================= filler (12): unrelated, different symptom ==============
    {
        "id": "dr_0019",
        "title": "Thermal derating on a power regulator",
        "role": "filler",
        "body": (
            "A buck regulator ran hot under full load and shut down "
            "intermittently. The root cause was insufficient copper pour for heat "
            "spreading and a missing thermal via array under the package. The fix "
            "enlarged the pour and added thermal vias. No signal-integrity issue "
            "was involved."
        ),
    },
    {
        "id": "dr_0020",
        "title": "Silkscreen polarity error on a connector",
        "role": "filler",
        "body": (
            "A USB connector footprint had a reversed silkscreen polarity mark "
            "that caused assembly confusion. The pads and netlist were correct. "
            "The fix was a silkscreen correction in the next revision. Electrical "
            "performance was unaffected."
        ),
    },
    {
        "id": "dr_0021",
        "title": "Voltage droop from decoupling capacitor placement",
        "role": "filler",
        "body": (
            "A processor core rail showed voltage droop during load steps. The "
            "decoupling capacitors sat too far from the power pins, which raised "
            "loop inductance. The fix moved the capacitors within a few "
            "millimetres of the pins with short, direct traces. This is a "
            "power-integrity fix, unrelated to trace-to-trace coupling."
        ),
    },
    {
        "id": "dr_0022",
        "title": "Solder bridge on a fine-pitch package",
        "role": "filler",
        "body": (
            "A fine-pitch package showed shorts between two adjacent pins after "
            "reflow. The cause was excess solder paste and a stencil aperture too "
            "large for the pitch. The fix reduced the aperture and adjusted the "
            "paste volume. A manufacturing defect, not a design coupling issue."
        ),
    },
    {
        "id": "dr_0023",
        "title": "Clearance rule violation near a mounting hole",
        "role": "filler",
        "body": (
            "A routing pass left a trace inside the keep-out around a mounting "
            "hole, flagged by the design-rule check. The fix rerouted the trace "
            "to restore clearance. This is a mechanical clearance rule, not a "
            "signal-integrity or coupling concern."
        ),
    },
    {
        "id": "dr_0024",
        "title": "Radiated-emissions failure from an unconnected shield",
        "role": "filler",
        "body": (
            "A board failed a radiated-emissions pre-scan. The cause was a "
            "connector shield left unconnected to chassis ground, so the shield "
            "could not drain. The fix tied the shield to ground through a short "
            "path. An EMC compliance fix, unrelated to trace-to-trace coupling "
            "inside the board."
        ),
    },
    {
        "id": "dr_0025",
        "title": "Tombstoned passive after reflow",
        "role": "filler",
        "body": (
            "A small chip resistor tombstoned during reflow, lifting one terminal "
            "off its pad. The cause was unequal pad thermal mass pulling the part "
            "upright. The fix balanced the pad geometry and adjusted the paste. "
            "An assembly defect with no bearing on signal coupling."
        ),
    },
    {
        "id": "dr_0026",
        "title": "Battery-backup current draw exceeding budget",
        "role": "filler",
        "body": (
            "A coin-cell backup domain drained faster than specified. The cause "
            "was a leakage path through a level-shifter left powered in standby. "
            "The fix gated the shifter's supply in the backup state. A "
            "low-power-design issue, unrelated to high-speed coupling."
        ),
    },
    {
        "id": "dr_0027",
        "title": "Wrong resistor value populating a divider",
        "role": "filler",
        "body": (
            "A voltage divider produced an out-of-range reference because a reel "
            "swap populated the wrong resistor value. The cause was a stores and "
            "labelling error, not the layout. The fix corrected the bill of "
            "materials and the feeder setup. No signal-integrity implication."
        ),
    },
    {
        "id": "dr_0028",
        "title": "Test point shorting to an adjacent net under probe pressure",
        "role": "filler",
        "body": (
            "During in-circuit test a probe deflected a test point into an "
            "adjacent pad, creating an intermittent short only while probed. The "
            "cause was test-point placement too close to a neighbouring feature. "
            "The fix moved the test point in the next revision. A fixture and "
            "placement issue, not trace coupling."
        ),
    },
    {
        "id": "dr_0029",
        "title": "Acid-trap warning from an acute trace angle",
        "role": "filler",
        "body": (
            "A fabrication review flagged an acute-angle trace junction as a "
            "potential acid trap that could over-etch. The cause was a routing "
            "artifact from an auto-router. The fix replaced the acute angle with "
            "two obtuse bends. A manufacturability fix, unrelated to coupling."
        ),
    },
    {
        "id": "dr_0030",
        "title": "Courtyard overlap between two adjacent components",
        "role": "filler",
        "body": (
            "A placement pass left two component courtyards overlapping, risking "
            "a mechanical collision at assembly. The cause was a density push in "
            "a tight region. The fix nudged one part and re-checked clearances. A "
            "placement and mechanical concern, not a signal one."
        ),
    },
]


def _markdown(case):
    """Render one case as a markdown document: title and body only. The role
    label is deliberately absent, so nothing about our seeding reaches the
    knowledge graph."""
    return f"# {case['title']}\n\n{case['body']}\n"


def generate_corpus(out_dir="data/autograph"):
    """Write each case as a markdown file, plus a separate manifest holding the
    bookkeeping. Returns the count per role.

    The markdown files are what you upload to AutoGraph. The manifest stays on
    our side: ingest.py reads it to tag each stored vector with its id and role.
    Validating that recall actually ranks close matches above near-misses and
    filler happens later, in the notebook, using those stored roles — it needs
    live embeddings, so it is kept out of this dependency-free generator.

    Both writes are UTF-8 explicitly, matching the encoding ingest.py reads with,
    so the corpus round-trips identically on any platform.
    """
    os.makedirs(out_dir, exist_ok=True)
    manifest = {}
    roles = {}
    for case in CASES:
        fname = f"{case['id']}.md"
        with open(os.path.join(out_dir, fname), "w", encoding="utf-8") as f:
            f.write(_markdown(case))
        manifest[case["id"]] = {
            "file": fname,
            "role": case["role"],
            "title": case["title"],
        }
        roles[case["role"]] = roles.get(case["role"], 0) + 1
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return roles


if __name__ == "__main__":
    counts = generate_corpus()
    total = sum(counts.values())
    print(f"wrote {total} markdown reports + manifest.json to data/autograph/")
    for role, n in sorted(counts.items()):
        print(f"  {role:12s} {n}")