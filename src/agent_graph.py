"""
agent_graph.py — the same investigation, driven by a framework.

agent.py decides the step order itself: traverse, score, judge, cite, in that
fixed sequence. This file does the same work but hands the control loop to a
framework. It exposes three tools and lets an LLM choose which to call, in what
order, and when to stop.

  traverse_and_score  - run the traversal and the severity score, return the
                        ranked candidate adjacencies.
  recall_similar      - vector-recall similar past defects for a description.
  cite_fix            - cite the fix from the knowledge graph via the Retriever.

Same tools, same database, same answer. The difference is who drives the loop:
the fixed resolver in agent.py, or the LLM here. Showing both makes the point
that the engine is the data and the query, not the orchestration around it. A
reader who prefers a framework drops it in without changing the store or the
queries.

This variant reuses the tested functions in agent.py rather than
reimplementing them, so the two never drift. In particular, traverse_and_score
runs the traversal alone (pure graph + key-value) and lets the LLM call
recall_similar separately — that is the framework's job, to choose the order —
whereas the fixed resolver fuses recall and traversal in the one marquee query.
Both read the same collections and the same severity score.
"""

import os
import json

import config as C
import agent


def build_agent(db, oai):
    """Build a ReAct agent over the three tools. Returns a runnable graph.

    Imports are local so the rest of the pipeline does not depend on LangChain.
    If the framework is not installed, the caller sees a clear import error
    with the install line, and the fixed resolver in agent.py still works.
    """
    from langchain_core.tools import tool
    from langchain_openai import ChatOpenAI
    try:
        from langchain.agents import create_agent as _create_agent
    except ImportError:
        from langgraph.prebuilt import create_react_agent as _create_agent

    @tool
    def traverse_and_score(victim_key: str) -> str:
        """Traverse the design graph from a victim net and return the adjacent
        nets ranked by severity, with each one's spacing, parallel length,
        layer, and score. Use this first to get the physically real
        candidates."""
        # Traversal only (the split query): pure graph + key-value. The severity
        # score reads the aggressor's class and activity from each candidate,
        # exactly as the fixed resolver does.
        cands = list(db.aql.execute(
            agent.CANDIDATES_Q,
            bind_vars={"victim": f"{C.NETS}/{victim_key}", "graph": C.GRAPH_NAME},
        ))
        for c in cands:
            c["severity"], c["severity_parts"] = agent.severity(c)
        cands.sort(key=lambda c: (c["severity"], -c["spacing_mm"], c["parallel_len_mm"]),
                   reverse=True)
        agent.rank_labels(cands)
        return json.dumps([
            {"aggressor_key": c["aggressor_key"], "aggressor_name": c["aggressor_name"],
             "aggressor_class": c["aggressor_class"], "spacing_mm": c["spacing_mm"],
             "parallel_len_mm": c["parallel_len_mm"], "layer": c["layer"],
             "severity": c["severity"], "rank_label": c.get("rank_label")}
            for c in cands
        ])

    @tool
    def recall_similar(description: str) -> str:
        """Recall up to three similar past defect cases by vector similarity.
        Use this to check whether the geometry matches a known crosstalk
        pattern or a look-alike with a different cause."""
        vec = agent.embed(oai, description)
        rows = db.aql.execute(
            "FOR d IN defect_reports "
            "SORT APPROX_NEAR_COSINE(d.embedding, @v) DESC LIMIT 3 "
            "RETURN {title: d.title, text: d.text}",
            bind_vars={"v": vec},
        )
        return json.dumps(list(rows))

    @tool
    def cite_fix(cause_text: str) -> str:
        """Cite the fix from the knowledge graph for the chosen cause via the
        Retriever (Unified Search). Call this last, after choosing the aggressor
        that best explains the symptom."""
        return json.dumps(agent.cite_fix_via_retriever(cause_text, deep=False, db=db, oai=oai))

    system = (
        "You are a signal-integrity crosstalk investigator. Given a victim net, "
        "find whether a neighbouring net couples into it and explain which one, "
        "then cite the fix. Order: 1) call traverse_and_score for the physically "
        "real candidates; 2) call recall_similar with a short description of the "
        "victim's symptom and geometry; 3) choose the single adjacency whose "
        "geometry and recalled cases agree, preferring a close, long, same-layer "
        "run; 4) call cite_fix with a short description of that chosen cause; "
        "5) give a final assessment: the chosen aggressor, why it explains the "
        "symptom, the severity numbers, and the cited fix. State clearly that "
        "the choice is a hypothesis to confirm on hardware, not a proven verdict."
    )

    llm = ChatOpenAI(model=C.CHAT_MODEL, api_key=agent.OPENAI_API_KEY)
    tools = [traverse_and_score, recall_similar, cite_fix]
    try:
        return _create_agent(llm, tools, system_prompt=system)   # langchain v1
    except TypeError:
        return _create_agent(llm, tools, prompt=system)          # older create_react_agent


def run(db, oai, victim_key):
    """Run the framework agent on one victim net, streaming its tool choices so
    the reader can watch it reason."""
    graph = build_agent(db, oai)
    final = None
    for chunk in graph.stream(
        {"messages": [("user", f"Investigate victim net {victim_key}.")]},
        stream_mode="values",
    ):
        m = chunk["messages"][-1]
        if getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                print(f"  [agent picks tool] {tc['name']}")
        elif getattr(m, "type", "") == "ai" and m.content:
            final = m.content
    if final:
        print("\n=== FINAL ASSESSMENT (framework agent) ===")
        print(final)
    return final


if __name__ == "__main__":
    from dotenv import load_dotenv
    from arango import ArangoClient
    from openai import OpenAI
    load_dotenv()
    client = ArangoClient(hosts=os.environ["ARANGO_HOST"])
    db = client.db(os.environ["ARANGO_DB"],
                   username=os.environ["ARANGO_USER"],
                   password=os.environ["ARANGO_PASSWORD"])
    oai = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    import sys
    run(db, oai, sys.argv[1] if len(sys.argv) > 1 else "net_15")