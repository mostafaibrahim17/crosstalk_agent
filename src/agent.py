"""
agent.py — the resolver: recall, traverse, judge, cite.

Given a victim net and a symptom, it:
  1. recalls similar past defects by vector search,
  2. traverses the graph out from the victim to its adjacent nets, reading each
     adjacency's physical parameters,
  3. scores every candidate adjacency with the severity score and labels each
     one's rank relative to the strongest,
  4. hands the scored candidates and the recalled cases to a judge, one live LLM
     step that picks the adjacency best explaining the symptom,
  5. cites the fix for the chosen cause through the Retriever,
  6. returns a ranked probable cause with reasoning, evidence, a confidence and
     its derived tier, and the data gaps behind that confidence.

On the query shape. All three data shapes live in one store, and the marquee
resolves the vector recall and the graph traversal in ONE AQL round trip: the
APPROX_NEAR_COSINE recall runs in its own subquery — where its SORT + LIMIT is
the top operation of that loop, the shape the vector index requires — the
traversal runs in a second subquery, and a single RETURN hands both back. That
is the multimodel payoff: document + vector recall and graph traversal resolved
together, over one store, with no application-side join. If a deployment's engine
rejects APPROX_NEAR_COSINE inside a subquery (older experimental builds required
it as the outermost SORT), _run_marquee falls back to two calls and prints which
path ran, so the claim always matches what the server actually did.

The one hard line: what the graph PROVES stays separate from what the agent
INTERPRETS. The traversal and parameters are fact. The judge's pick is a
hypothesis, and the finding's tier is capped at "corroborated" until an engineer
confirms it on hardware (see derive_tier and config.HARDWARE_VERIFIED).
"""

import os
import re
import json
import requests

from openai import OpenAI

import config as C


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


# --- The marquee: one round trip -------------------------------------------
# The vector recall and the graph traversal in a single AQL pass. Each runs in
# its own subquery so APPROX_NEAR_COSINE keeps its own top-level SORT + LIMIT,
# then one RETURN hands both results back together.
MARQUEE = """
LET similar = (
    FOR d IN defect_reports
        SORT APPROX_NEAR_COSINE(d.embedding, @symptom_vec) DESC
        LIMIT @k
        RETURN { id: d._key, title: d.title, text: d.text }
)
LET candidates = (
    FOR neighbour, edge IN 1..1 OUTBOUND @victim GRAPH @graph
        RETURN {
            aggressor_key: neighbour._key,
            aggressor_name: neighbour.name,
            aggressor_class: neighbour.net_class,
            aggressor_activity: neighbour.activity,
            spacing_mm: edge.spacing_mm,
            parallel_len_mm: edge.parallel_len_mm,
            layer: edge.layer,
            trace_width_mm: edge.trace_width_mm
        }
)
RETURN { similar: similar, candidates: candidates }
"""

# The two sub-shapes, kept as standalone queries for the two-call fallback and
# for the framework variant (agent_graph.py) that calls the traversal alone.
SIMILAR_Q = """
FOR d IN defect_reports
    SORT APPROX_NEAR_COSINE(d.embedding, @symptom_vec) DESC
    LIMIT @k
    RETURN { id: d._key, title: d.title, text: d.text }
"""

# aggressor_class and aggressor_activity are read from the NEIGHBOUR node, not
# the edge. The neighbour is the aggressor for THIS traversal direction, so its
# own net_class is always correct; a value frozen on the edge at ingest time
# would be right in one direction and wrong in the other. The geometry
# (spacing, length, layer, width) is direction-symmetric and stays on the edge.
CANDIDATES_Q = """
FOR neighbour, edge IN 1..1 OUTBOUND @victim GRAPH @graph
    RETURN {
        aggressor_key: neighbour._key,
        aggressor_name: neighbour.name,
        aggressor_class: neighbour.net_class,
        aggressor_activity: neighbour.activity,
        spacing_mm: edge.spacing_mm,
        parallel_len_mm: edge.parallel_len_mm,
        layer: edge.layer,
        trace_width_mm: edge.trace_width_mm
    }
"""


def _run_marquee(db, symptom_vec, victim_key, k):
    """Run recall + traversal as ONE AQL round trip; return (similar, candidates).

    Attempts the single marquee query first. If the engine rejects
    APPROX_NEAR_COSINE inside a subquery, falls back to two calls and prints
    which path ran, so the tutorial's "one query" claim always reflects reality.
    """
    victim_id = f"{C.NETS}/{victim_key}"
    try:
        rows = list(db.aql.execute(
            MARQUEE,
            bind_vars={"symptom_vec": symptom_vec, "k": k,
                       "victim": victim_id, "graph": C.GRAPH_NAME},
        ))
        out = rows[0] if rows else {"similar": [], "candidates": []}
        print("  [marquee: one AQL round trip]")
        return out.get("similar", []), out.get("candidates", [])
    except Exception as e:
        print(f"  [marquee: vector subquery not supported here "
              f"({type(e).__name__}); two-call fallback]")
        similar = list(db.aql.execute(
            SIMILAR_Q, bind_vars={"symptom_vec": symptom_vec, "k": k}))
        candidates = list(db.aql.execute(
            CANDIDATES_Q, bind_vars={"victim": victim_id, "graph": C.GRAPH_NAME}))
        return similar, candidates


def embed(oai, text):
    """Embed with the same model ingestion used, so dimensions match."""
    return oai.embeddings.create(model=C.EMBEDDING_MODEL, input=text).data[0].embedding


# --- The severity score -----------------------------------------------------

def _normalize_spacing(spacing_mm, trace_width_mm):
    """Closer than the 3W edge-to-edge rule is worst (1); at twice the rule it
    fades to 0. Grounds the score in a real design rule."""
    rule_mm = C.SPACING_RULE_EDGE_MULTIPLE * trace_width_mm
    if rule_mm <= 0:
        return 0.0
    ratio = spacing_mm / rule_mm
    if ratio <= 1.0:
        return 1.0
    if ratio >= 2.0:
        return 0.0
    return 1.0 - (ratio - 1.0)


def _normalize_length(parallel_len_mm, ref_mm=None):
    ref = ref_mm if ref_mm is not None else C.LENGTH_REF_MM
    return min(parallel_len_mm / ref, 1.0)


def severity(cand):
    """Return (score, parts) for one candidate adjacency.

    The loudness term is the aggressor's effective strength — how loud its class
    is, gated by how active it is — computed in config.aggressor_strength so all
    the ranking policy lives in one file. An idle aggressor contributes nothing
    to the loudness term no matter how bad its geometry.
    """
    s = _normalize_spacing(cand["spacing_mm"], cand["trace_width_mm"])
    l = _normalize_length(cand["parallel_len_mm"])
    strength = C.aggressor_strength(cand["aggressor_class"], cand.get("aggressor_activity"))
    w = C.SEVERITY_WEIGHTS
    parts = {
        "spacing": round(w["spacing"] * s, 4),
        "length": round(w["length"] * l, 4),
        "loudness": round(w["loudness"] * strength, 4),
    }
    return round(sum(parts.values()), 4), parts


def rank_labels(candidates):
    """Attach a RELATIVE rank label to each scored candidate: the strongest is
    'leading', any within C.LEADING_MARGIN of it is 'plausible', the rest
    'unlikely'. Candidates must already be sorted by severity descending."""
    if not candidates:
        return candidates
    top = candidates[0]["severity"]
    for i, c in enumerate(candidates):
        if i == 0:
            c["rank_label"] = C.RANK_LABELS["leading"]
        elif top - c["severity"] <= C.LEADING_MARGIN:
            c["rank_label"] = C.RANK_LABELS["plausible"]
        else:
            c["rank_label"] = C.RANK_LABELS["unlikely"]
    return candidates


# --- The confidence tier (DERIVED, not read off the judge) ------------------

def derive_tier(judge_conf, candidates, corroboration, hardware_verified=None):
    """Derive the finding's confidence tier from evidence, not from the LLM alone.

    The judge reports a high/medium/low confidence, but that number swings
    between runs on identical data, and a clean geometry tempts it toward
    "high" — which would read as "confirmed" on a finding nothing has verified.
    So we derive the tier from three signals instead, and gate the top one:

      - severity gap : how decisively the leading candidate beats the runner-up.
                       This is GRAPH-PROVEN and stable — the real signal.
      - corroboration: did the cited fix independently land on the crosstalk
                       mechanism? Two surfaces agreeing is worth more than one
                       model's self-assessment.
      - judge_conf   : the judge's own confidence, used only as a soft floor
                       (a "low" from the judge still excludes).
      - hardware_verified: whether an engineer has confirmed on silicon. In the
                       tutorial this is always False (config.HARDWARE_VERIFIED),
                       so "confirmed" is structurally unreachable and the honest
                       ceiling is "corroborated". That is the point, not a bug.

    Returns one of config.TIER_{CONFIRMED,CORROBORATED,CANDIDATE,EXCLUDED}.
    """
    if hardware_verified is None:
        hardware_verified = C.HARDWARE_VERIFIED

    # No candidate at all, or the judge itself found nothing adjacency-shaped:
    # crosstalk does not explain the symptom.
    if not candidates or judge_conf == "low":
        return C.TIER_EXCLUDED

    # How decisively does the leading candidate beat the runner-up? A wide gap
    # is graph-proven and stable; a narrow one means several plausible suspects
    # remain in play.
    top = candidates[0]["severity"]
    second = candidates[1]["severity"] if len(candidates) > 1 else 0.0
    decisive = (top - second) >= C.DECISIVE_SEVERITY_GAP

    mechanism_agrees = bool((corroboration or {}).get("mechanism_match"))

    # Only a real hardware or log check earns "confirmed". Never reachable in
    # the tutorial, by design.
    if hardware_verified and judge_conf == "high" and decisive and mechanism_agrees:
        return C.TIER_CONFIRMED

    # The strongest honest tier without silicon: the geometry decisively points
    # at one aggressor AND an independent citation converged on the mechanism.
    if decisive and mechanism_agrees and judge_conf in ("high", "medium"):
        return C.TIER_CORROBORATED

    # Everything else is a live hypothesis worth chasing, not yet corroborated.
    return C.TIER_CANDIDATE


# --- The judge --------------------------------------------------------------
# The recalled cases are passed WITHOUT their seeding labels, so the judge
# cannot read the answer off a label; it must reason from symptom and geometry.

JUDGE_SYSTEM = (
    "You are a signal-integrity investigator. You are given a victim net, its "
    "symptom, a ranked list of physically real adjacent nets with their measured "
    "spacing, parallel length, layer, and severity score, and a set of similar "
    "past defect cases. Choose the single adjacency that best explains the "
    "symptom. You may only choose from the candidates given; you may not invent "
    "one. Prefer the candidate whose geometry and recalled cases agree. Be "
    "explicit that your choice is a hypothesis to confirm on hardware, not a "
    "proven verdict. Respond as JSON with keys: chosen_aggressor_key, reasoning, "
    "confidence (high|medium|low), data_gaps (list of what would raise "
    "confidence)."
)


def judge(oai, victim_name, symptom, scored_candidates, similar):
    payload = {
        "victim_net": victim_name,
        "symptom": symptom,
        "candidates": [
            {
                "aggressor_key": c["aggressor_key"],
                "aggressor_name": c["aggressor_name"],
                "aggressor_class": c["aggressor_class"],
                "spacing_mm": c["spacing_mm"],
                "parallel_len_mm": c["parallel_len_mm"],
                "layer": c["layer"],
                "severity": c["severity"],
            }
            for c in scored_candidates
        ],
        "similar_cases": [{"title": s["title"], "text": s["text"]} for s in similar],
    }
    resp = oai.chat.completions.create(
        model=C.CHAT_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": json.dumps(payload)},
        ],
    )
    return json.loads(resp.choices[0].message.content)


# --- The cited fix (Retriever) ----------------------------------------------

def _retriever_base_with_port(base):
    """Add the ArangoDB API port :8529 when the host carries no explicit port."""
    b = (base or "").rstrip("/")
    m = re.match(r"^(https?://[^/:]+)(:\d+)?$", b)
    return m.group(1) + ":8529" if (m and not m.group(2)) else b


_JWT = None


def _get_jwt():
    """Authenticate against Arango and cache a JWT. The Retriever endpoint is
    protected, so a bare POST 401s; we exchange the Arango user/password for a
    token, exactly as the reference agent does."""
    global _JWT
    if _JWT:
        return _JWT
    base = _retriever_base_with_port(os.getenv("ARANGO_HOST"))
    r = requests.post(
        f"{base}/_open/auth",
        json={"username": os.getenv("ARANGO_USER"), "password": os.getenv("ARANGO_PASSWORD")},
        timeout=20,
    )
    r.raise_for_status()
    _JWT = r.json()["jwt"]
    return _JWT


def _extract_answer(data):
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for k in ("result", "answer", "response", "output", "text", "content"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                return v
        for k in ("data", "result", "response"):
            v = data.get(k)
            if isinstance(v, dict):
                inner = _extract_answer(v)
                if inner:
                    return inner
        return json.dumps(data)[:1200]
    return str(data)[:1200]


def cite_fix_via_retriever(cause_text, deep=False, db=None, oai=None):
    """Return a cited fix for the judge's chosen cause via the Arango Retriever.

    Endpoint (resolved at call time so a loaded .env is in effect):
      {host}:8529/graphrag/retriever/{serviceIdPostfix}/v1/graphrag-query
    where serviceIdPostfix is the last hyphen-segment of RETRIEVER_ID
    (arangodb-graphrag-retriever-ebiw4 -> ebiw4). JWT-authenticated.

    query_type 3 = Unified Search (fast default); query_type 2 + planner = Deep
    Search (deep=True). Falls back to local vector search if unset or on error.
    db/oai are threaded through so the fallback can reuse live connections.
    """
    rid = os.getenv("RETRIEVER_ID")
    base = os.getenv("RETRIEVER_BASE") or os.getenv("ARANGO_HOST")
    if rid and base:
        postfix = rid.split("-")[-1]
        url = os.getenv("RETRIEVER_URL") or (
            f"{_retriever_base_with_port(base)}/graphrag/retriever/"
            f"{postfix}/v1/graphrag-query"
        )
        body = {
            "query": (
                "A high-speed net shows this symptom and a likely coupling "
                f"cause: {cause_text}. What was the root cause and the fix in "
                "similar past cases? Cite the specific defect reports."
            ),
            "query_type": 2 if deep else 3,
            "level": 1,
            "provider": 0,
        }
        if deep:
            body["use_llm_planner"] = True
        try:
            headers = {"Authorization": f"Bearer {_get_jwt()}",
                       "Content-Type": "application/json"}
            r = requests.post(url, json=body, headers=headers, timeout=420 if deep else 180)
            r.raise_for_status()
            return {
                "path": "retriever_deep" if deep else "retriever_unified",
                "answer": _extract_answer(r.json()),
            }
        except Exception as e:
            print(f"  [retriever error: {type(e).__name__}: {e}] -> local fallback")
            return _local_fix_fallback(cause_text, db=db, oai=oai)

    print("  [retriever not configured: RETRIEVER_BASE/RETRIEVER_ID unset] -> local fallback")
    return _local_fix_fallback(cause_text, db=db, oai=oai)


def _local_fix_fallback(cause_text, db=None, oai=None):
    """Grounded fallback: the closest past report's fix, labelled as a local
    corpus passage rather than a knowledge-graph citation. Reuses passed db/oai."""
    if oai is None:
        oai = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    if db is None:
        from arango import ArangoClient
        client = ArangoClient(hosts=os.environ["ARANGO_HOST"])
        db = client.db(
            os.environ["ARANGO_DB"],
            username=os.environ["ARANGO_USER"],
            password=os.environ["ARANGO_PASSWORD"],
        )
    vec = embed(oai, cause_text)
    rows = list(db.aql.execute(
        """
        FOR d IN defect_reports
            SORT APPROX_NEAR_COSINE(d.embedding, @v) DESC
            LIMIT 1
            RETURN { title: d.title, text: d.text }
        """,
        bind_vars={"v": vec},
    ))
    if not rows:
        return {"path": "local_fallback", "answer": "No matching past case found."}
    hit = rows[0]
    return {
        "path": "local_fallback",
        "answer": (
            f"Closest past case: {hit['title']}. {hit['text']} "
            "(Local corpus passage; deploy the Retriever for a knowledge-graph citation.)"
        ),
    }


# --- Corroboration -----------------------------------------------------------
# Two independent surfaces should agree: the graph+judge chose an aggressor and
# the crosstalk mechanism, and the Retriever cited past reports. If the cited
# answer references the crosstalk mechanism (and ideally the chosen aggressor),
# the two converged without being told each other's answer. That agreement is
# the confidence signal a vector-only tool cannot produce, and it is one of the
# inputs derive_tier() uses to reach "corroborated".


def corroborate(chosen, citation):
    """Agreement verdict between the judge's pick and the citation.

    Lightweight textual convergence check (not a second model call): does the
    cited fix mention the crosstalk mechanism, and the chosen aggressor's name?"""
    answer = (citation or {}).get("answer", "") or ""
    text = answer.lower()
    mechanism_terms = ["crosstalk", "coupling", "parallel run", "far-end", "near-end"]
    mechanism_match = any(t in text for t in mechanism_terms)
    aggressor_match = False
    matched_on = []
    if chosen:
        name = (chosen.get("aggressor_name") or "").lower()
        short = name.split("/")[-1] if name else ""
        for token in {name, short}:
            if token and token in text:
                aggressor_match = True
                matched_on.append(token)
    return {
        "agree": bool(mechanism_match),
        "mechanism_match": mechanism_match,
        "aggressor_named_in_citation": aggressor_match,
        "matched_on": matched_on,
    }


# --- Auto mode ---------------------------------------------------------------
# Instead of being handed a victim, scan the whole board for the single most
# dangerous adjacency and diagnose that net: "point it at the board and it finds
# the hero." Same severity function over every edge.

# Same rule as CANDIDATES_Q: the aggressor is the edge's _to net, so read its
# class and activity from that node. Both directions of every pair are stored,
# so scanning all edges scores each ordered (victim -> aggressor) pair with the
# correct aggressor class.
ALL_EDGES_Q = """
FOR e IN adjacencies
    LET aggressor = DOCUMENT(e._to)
    RETURN {
        victim_key: PARSE_IDENTIFIER(e._from).key,
        aggressor_key: PARSE_IDENTIFIER(e._to).key,
        aggressor_name: aggressor.name,
        aggressor_class: aggressor.net_class,
        aggressor_activity: aggressor.activity,
        spacing_mm: e.spacing_mm,
        parallel_len_mm: e.parallel_len_mm,
        layer: e.layer,
        trace_width_mm: e.trace_width_mm
    }
"""


def worst_victim(db):
    """Scan every adjacency, score it, return the victim key of the worst one."""
    edges = list(db.aql.execute(ALL_EDGES_Q))
    if not edges:
        return None, None
    for e in edges:
        e["severity"], _ = severity(e)
    edges.sort(key=lambda e: (e["severity"], -e["spacing_mm"], e["parallel_len_mm"]),
               reverse=True)
    worst = edges[0]
    return worst["victim_key"], worst["severity"]


# --- The resolver -----------------------------------------------------------

def resolve(db, oai, victim_key, symptom, k=5, deep=False):
    """Diagnose one symptom on one victim net, end to end."""
    symptom_vec = embed(oai, symptom)

    # Recall + traversal in one AQL round trip (two-call fallback inside).
    similar, candidates = _run_marquee(db, symptom_vec, victim_key, k)

    for c in candidates:
        c["severity"], c["severity_parts"] = severity(c)

    # Sort by severity; break ties by physics: a closer trace couples more, and
    # among equally close ones a longer parallel run couples more.
    candidates.sort(
        key=lambda c: (c["severity"], -c["spacing_mm"], c["parallel_len_mm"]),
        reverse=True,
    )
    rank_labels(candidates)

    victim_name = _victim_name(db, victim_key)

    if candidates:
        verdict = judge(oai, victim_name, symptom, candidates, similar)
    else:
        verdict = {
            "chosen_aggressor_key": None,
            "reasoning": ("No adjacency was found for this net, so crosstalk does "
                          "not explain the symptom. Look to a non-adjacency cause "
                          "(impedance, return path, supply)."),
            "confidence": "low",
            "data_gaps": ["no candidate adjacencies in the graph for this net"],
        }

    chosen_key = verdict.get("chosen_aggressor_key")
    chosen = next((c for c in candidates if c["aggressor_key"] == chosen_key), None)
    if chosen:
        cause_text = (
            f"victim {victim_name} coupling from {chosen['aggressor_name']}, "
            f"spacing {chosen['spacing_mm']}mm, parallel {chosen['parallel_len_mm']}mm "
            f"on {chosen['layer']}; symptom: {symptom}"
        )
    else:
        cause_text = f"victim {victim_name}; symptom: {symptom}"
    citation = cite_fix_via_retriever(cause_text, deep=deep, db=db, oai=oai)

    # Corroboration first, because the derived tier reads it. Two independent
    # surfaces (graph+judge, and the Retriever's citation) converging on the
    # crosstalk mechanism is a stronger signal than the judge's self-report.
    corroboration = corroborate(chosen, citation)

    # Confidence tier: DERIVED from the graph-proven severity gap, the
    # corroboration, and the judge's confidence — and capped at "corroborated"
    # while config.HARDWARE_VERIFIED is False. The judge's raw confidence is
    # kept in verdict for transparency, but does not set the tier by itself.
    conf = verdict.get("confidence", "low")
    confidence_tier = derive_tier(conf, candidates, corroboration)

    return {
        "victim_key": victim_key,
        "victim_name": victim_name,
        "symptom": symptom,
        "candidates": candidates,
        "similar_cases": similar,
        "judge": verdict,
        "confidence_tier": confidence_tier,
        "cited_fix": citation,
        "corroboration": corroboration,
    }


def _victim_name(db, victim_key):
    doc = db.collection(C.NETS).get(victim_key)
    return doc["name"] if doc else victim_key


def find_escalation(db, oai, victim_key, symptom, k=5):
    """Alias for the framework variant: same resolve, flatter shape."""
    r = resolve(db, oai, victim_key, symptom, k=k)
    top = r["candidates"][0] if r["candidates"] else None
    return {
        "victim_name": r["victim_name"],
        "candidate_count": len(r["candidates"]),
        "top_candidate": top,
        "chosen": r["judge"].get("chosen_aggressor_key"),
        "confidence": r["judge"].get("confidence"),
        "confidence_tier": r["confidence_tier"],
    }


# --- Run it -----------------------------------------------------------------

def _print_result(r):
    print(f"\n=== VICTIM ===\n{r['victim_name']} ({r['victim_key']})  symptom: {r['symptom']}")
    print(f"\n=== CANDIDATES (ranked, proved) ===")
    for c in r["candidates"]:
        print(f"  {c['aggressor_name'][:34]:36s} sev {c['severity']:<6} "
              f"[{c.get('rank_label','')}]  {c['aggressor_class']:<7} "
              f"gap {c['spacing_mm']}mm  len {c['parallel_len_mm']}mm  {c['layer']}")
    if not r["candidates"]:
        print("  (none — no adjacency for this net)")
    print(f"\n=== RECALLED CASES ===")
    for s in r["similar_cases"]:
        print(f"  {s['title']}")
    v = r["judge"]
    print(f"\n=== JUDGE (interpretation, a hypothesis) ===")
    print(f"  chosen: {v.get('chosen_aggressor_key')}")
    print(f"  judge confidence: {v.get('confidence')}  ->  finding tier [{r.get('confidence_tier','candidate')}]")
    print(f"  reasoning: {v.get('reasoning')}")
    print(f"  data_gaps: {v.get('data_gaps')}")
    print(f"\n=== CITED FIX ({r['cited_fix']['path']}) ===")
    print(r["cited_fix"]["answer"])
    co = r.get("corroboration", {})
    print(f"\n=== CORROBORATION (independent surfaces agree?) ===")
    print(f"  mechanism converged : {co.get('mechanism_match')}")
    print(f"  aggressor cited     : {co.get('aggressor_named_in_citation')} {co.get('matched_on') or ''}")
    print(f"  verdict             : {'AGREE - graph and citation point at the same crosstalk story' if co.get('agree') else 'no clear agreement'}")
    print(f"\n  (tier is derived from the severity gap + corroboration, and capped "
          f"at '{C.TIER_CORROBORATED}' until hardware confirms it.)")


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    from arango import ArangoClient

    load_dotenv()
    client = ArangoClient(hosts=os.environ["ARANGO_HOST"])
    db = client.db(
        os.environ["ARANGO_DB"],
        username=os.environ["ARANGO_USER"],
        password=os.environ["ARANGO_PASSWORD"],
    )
    oai = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    arg = sys.argv[1] if len(sys.argv) > 1 else "net_35"
    deep = "--deep" in sys.argv
    if arg == "auto":
        victim, worst_sev = worst_victim(db)
        if victim is None:
            raise SystemExit("No adjacencies in the graph - run ingest first.")
        print(f"Auto mode: worst adjacency on the board is on "
              f"{_victim_name(db, victim)} ({victim}), severity {worst_sev}.")
    else:
        victim = arg
    symptom = "frequency variation and rising bit error rate on a high-speed data lane"

    result = resolve(db, oai, victim, symptom, deep=deep)
    _print_result(result)