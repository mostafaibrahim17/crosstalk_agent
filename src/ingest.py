"""
ingest.py — load the board and the defect corpus into one Arango database.

This is where three data shapes land in a single store, which is the whole
argument of the tutorial. The board's geometry becomes a graph (nets as nodes,
adjacencies as edges). The past defect reports become vectors under a vector
index. Both live in the same database, so one query can later walk the graph
and search the vectors without an application-side join.

The functions are small and idempotent on purpose. Run the notebook top to
bottom, or re-run one cell, and the state stays correct.

What each step builds:
  setup_collections  - the document, edge, and vector collections, and the
                       named graph the traversal walks.
  load_board         - parse the KiCad board and write nets + adjacencies.
  load_defects       - embed the synthetic defect corpus and store the vectors.
  build_vector_index - the index that makes APPROX_NEAR_COSINE fast.

We keep the schema in plain Python here, not an ontology layer, because the
tutorial is about the query path, not schema modeling. A reader can see every
field in one place.
"""

import os
import json
import glob

from arango import ArangoClient
from openai import OpenAI

import config as C
from parse_board import parse_board


# --- Collections and graph --------------------------------------------------

def setup_collections(db):
    """Create the collections and the named graph. Safe to re-run.

    Three shapes, one store:
      - nets          : document collection, the nodes.
      - adjacencies   : edge collection, the close parallel runs.
      - defect_reports: document collection holding the embedded past cases.

    The named graph binds nets and adjacencies so a traversal can walk OUTBOUND
    from a victim net to its neighbors in one AQL step.
    """
    if not db.has_collection(C.NETS):
        db.create_collection(C.NETS)
    if not db.has_collection(C.ADJACENCIES):
        db.create_collection(C.ADJACENCIES, edge=True)
    if not db.has_collection(C.DEFECTS):
        db.create_collection(C.DEFECTS)

    if not db.has_graph(C.GRAPH_NAME):
        g = db.create_graph(C.GRAPH_NAME)
        g.create_edge_definition(
            edge_collection=C.ADJACENCIES,
            from_vertex_collections=[C.NETS],
            to_vertex_collections=[C.NETS],
        )
    return db


# --- Board -> graph ---------------------------------------------------------

def load_board(db, board_path):
    """Parse the KiCad board and write its nets and adjacencies.

    The nets become nodes keyed by net id, each carrying its own class and
    activity. The adjacencies become edges carrying only the geometry the
    severity score reads: spacing, parallel length, layer, and trace width.

    Note on what the edge does NOT carry. The aggressor's class and activity are
    properties of a net, not of the pair, and they are read at query time from
    whichever node is the aggressor for that traversal direction. We deliberately
    do not freeze them onto the edge: an edge is stored in both directions, so a
    frozen aggressor_class would be correct walking one way and wrong walking the
    other. Geometry is direction-symmetric (the gap between two traces is the
    same from either end), so it lives on the edge; class and activity are
    direction-dependent, so they stay on the nodes.
    """
    nets, adjacencies = parse_board(board_path)

    net_col = db.collection(C.NETS)
    adj_col = db.collection(C.ADJACENCIES)
    net_col.truncate()
    adj_col.truncate()

    net_col.import_bulk([
        {
            "_key": n.key,
            "net_id": n.net_id,
            "name": n.name,
            "net_class": n.net_class,
            "activity": n.activity,
        }
        for n in nets
    ])

    # Store the same adjacency in both directions so a traversal from either net
    # finds the other; crosstalk coupling is mutual, and the engineer may name
    # either side as the victim. The edge carries only direction-symmetric
    # geometry — the aggressor's class/activity are read from the neighbour node
    # at query time (see CANDIDATES_Q / ALL_EDGES_Q in agent.py).
    edges = []
    for a in adjacencies:
        base = {
            "spacing_mm": a.spacing_mm,
            "parallel_len_mm": a.parallel_len_mm,
            "layer": a.layer,
            "trace_width_mm": a.trace_width_mm,
        }
        edges.append({
            "_from": f"{C.NETS}/{a.victim_key}",
            "_to": f"{C.NETS}/{a.aggressor_key}",
            **base,
        })
        edges.append({
            "_from": f"{C.NETS}/{a.aggressor_key}",
            "_to": f"{C.NETS}/{a.victim_key}",
            **base,
        })
    adj_col.import_bulk(edges)

    return {"nets": len(nets), "adjacencies": len(adjacencies)}


# --- Defect corpus -> vectors -----------------------------------------------

def embed(oai, text):
    """Embed one string with the shared model. Ingestion and query call this
    same function, so their vectors always share dimensions."""
    resp = oai.embeddings.create(model=C.EMBEDDING_MODEL, input=text)
    return resp.data[0].embedding


def load_defects(db, oai, corpus_dir):
    """Embed every defect report in the corpus and store it with its vector.

    The corpus is markdown, the shape AutoGraph ingests: one .md per case, with
    a title and an engineer's account of the symptom, the geometry, the root
    cause, and the fix. The seeding label (close_match / near_miss / filler)
    lives only in manifest.json, never in the markdown, so it cannot leak into
    the AutoGraph knowledge graph. We read the label from the manifest here and
    keep it on the vector document for offline recall validation, not for the
    judge.

    We embed the full report text so a new symptom recalls cases that read like
    it. That recall is what lets the engine say "this pattern was crosstalk
    before," and it feeds the cited fix.

    Returns the count and the embedding dimension, so the caller can build the
    vector index to match without hard-coding a number.
    """
    col = db.collection(C.DEFECTS)
    col.truncate()

    manifest_path = os.path.join(corpus_dir, "manifest.json")
    manifest = json.load(open(manifest_path)) if os.path.exists(manifest_path) else {}

    docs = []
    dim = None
    for path in sorted(glob.glob(os.path.join(corpus_dir, "*.md"))):
        doc_id = os.path.splitext(os.path.basename(path))[0]
        text = open(path, encoding="utf-8").read().strip()
        meta = manifest.get(doc_id, {})
        vec = embed(oai, text)
        dim = len(vec)
        docs.append({
            "_key": doc_id,
            "title": meta.get("title", doc_id),
            "text": text,
            "role": meta.get("role", "unknown"),   # our bookkeeping only
            "embedding": vec,
        })
    if docs:
        col.import_bulk(docs)
    return {"defect_reports": len(docs), "dimension": dim}


def build_vector_index(db, dimension=None):
    """Create the vector index that powers APPROX_NEAR_COSINE.

    The index trains a quantizer over the stored vectors, so this call can take
    a moment. Without it, similarity search still works but scans every vector;
    with it, recall stays fast as the corpus grows. nLists is kept small
    because the tutorial corpus is small.

    The dimension is read from a stored vector rather than hard-coded, so it can
    never drift from the embedding model set in config: change EMBEDDING_MODEL,
    re-embed, and the index is built to match whatever the model produced. The
    `dimension` argument is only a fallback for when the collection is empty.
    """
    col = db.collection(C.DEFECTS)

    # Prefer the dimension of the vectors actually stored. This keeps the index
    # in lock-step with the embedding model without repeating the number.
    if dimension is None:
        sample = list(db.aql.execute(
            f"FOR d IN {C.DEFECTS} FILTER d.embedding != null "
            "LIMIT 1 RETURN LENGTH(d.embedding)"))
        if sample:
            dimension = sample[0]
    if dimension is None:
        raise ValueError(
            "No embeddings found and no dimension given; run load_defects first.")

    # Remove any prior vector index so re-runs stay clean.
    for idx in col.indexes():
        if idx.get("type") == "vector":
            col.delete_index(idx["id"])
    col.add_index({
        "type": "vector",
        "fields": ["embedding"],
        "params": {
            "metric": "cosine",
            "dimension": dimension,
            "nLists": 2,
        },
    })
    return {"dimension": dimension}


# --- One call to build everything ------------------------------------------

def build_all(db, oai, board_path, corpus_dir):
    """Run the whole ingest in order and report the counts.

    Order matters: the vector index trains on the vectors, so load_defects must
    run before build_vector_index. We thread the embedding dimension straight
    from the load step into the index build, so the two can never disagree.
    """
    setup_collections(db)
    board_stats = load_board(db, board_path)
    defect_stats = load_defects(db, oai, corpus_dir)
    index_stats = build_vector_index(db, dimension=defect_stats.get("dimension"))
    return {**board_stats, **defect_stats, **index_stats}


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    client = ArangoClient(hosts=os.environ["ARANGO_HOST"])
    db = client.db(
        os.environ["ARANGO_DB"],
        username=os.environ["ARANGO_USER"],
        password=os.environ["ARANGO_PASSWORD"],
    )
    oai = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    stats = build_all(db, oai, "data/CM4IOUSB3.kicad_pcb", "data/autograph")
    print("ingest complete:", stats)