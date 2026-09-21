# Explainable Crosstalk Diagnosis on Arango

A signal-integrity agent that answers one question no single design-rule check can:
**which physical adjacency on a board explains a frequency variation on a data lane, and how
was that fault fixed before.**

A board defect is not a value in one table. It is a relationship between two nets: a power or
clock line running too close to a data line, over a long enough run to couple noise into it.
That relationship is an edge, not a row, which is why record-by-record checks pass while the
board keeps failing. This build models the board as a graph, gives that graph a memory of past
defects, and resolves both in a single query on the [Arango Contextual Data
Platform](https://arango.ai/products/contextual-data-platform).

---

## The hero fault

Point the engine at a real Raspberry Pi CM4 IO board and it finds, on its own, the worst
adjacency:

```
victim     /CM4_HighSpeed/DSI1_D0_N   (data)
aggressor  /CM4_HighSpeed/DSI0_C_P    (clock)
spacing 0.42 mm   parallel 43.3 mm   layer B.Cu   severity 0.587  [corroborated]
```

A DSI clock lane running 43 mm alongside a data lane from a different camera interface, 0.42 mm
apart on the bottom copper layer. Every trace passes its own check; the fault lives only in the
relationship between the two.

![Board severity map](assets/board_severity_map.png)

---

## How it works

Three data shapes live in one store, and one query reads all three:

- **A design graph** parsed from the real KiCad board: nets as nodes, close parallel runs as
  adjacency edges carrying spacing, length, layer, and trace width.
- **Vector recall** over a corpus of past defect reports, embedded for similarity search with
  `APPROX_NEAR_COSINE`.
- **A knowledge graph** built by AutoGraph from the same corpus, queried through the GraphRAG
  Retriever, that cites the fix behind a fault.

The agent embeds the symptom, recalls similar past defects, traverses the design graph, scores
each candidate adjacency, judges which one explains the symptom, and returns a cited fix. The
graph proves the geometry; the agent interprets which adjacency is the cause, and the finding is
capped at `corroborated` until hardware confirms it.

---

## Quickstart

```bash
# 1. clone and enter the repo
git clone <your-repo-url> crosstalk-agent
cd crosstalk-agent

# 2. create and activate a virtual environment
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 3. install dependencies
pip install -r requirements.txt

# 4. configure credentials
cp .env.example .env
#   then edit .env with your Arango, OpenAI, and (optional) Retriever details
```

`.env` holds the connection details:

```
ARANGO_HOST=https://<your-deployment>.arangodb.cloud
ARANGO_DB=crosstalk_v1
ARANGO_USER=root
ARANGO_PASSWORD=<your-password>
OPENAI_API_KEY=<your-key>

# Set after deploying the Retriever; until then the agent falls back to a local citation.
RETRIEVER_ID=
RETRIEVER_BASE=
```

Then build the data and run the diagnosis:

```bash
python src/prepare_autograph.py     # write the synthetic defect corpus (30 reports)
python src/ingest.py                # parse the board, load the graph + vectors + index
python src/agent.py auto            # scan the board and diagnose the worst adjacency
python src/agent.py auto --deep     # same, with Deep Search for a fuller cited playbook
```

To build the knowledge graph the Retriever cites from, upload `data/autograph/*.md` to
**AutoGraph Studio** in the Arango web interface, build the graph with
`text-embedding-3-small`, deploy the Retriever, and set `RETRIEVER_ID` / `RETRIEVER_BASE` in
`.env`. The pipeline runs either way; without the Retriever it uses a local corpus citation.

---

## The notebook

`crosstalk_analysis.ipynb` runs the whole pipeline end to end, cell by cell, and closes with
five figures saved to `assets/`:

| figure | what it shows |
|---|---|
| `board_severity_map.png` | every real adjacency, colored by class, sized by severity |
| `severity_ranking.png` | the ten adjacencies ranked; the hero clears the field |
| `severity_decomposition.png` | spacing / length / loudness contribution per candidate |
| `danger_region.png` | spacing vs parallel length, with the 3W danger line |
| `recall_validation.png` | recall ranks close-match cases above near-misses and filler |

---

## Repository layout

```
src/
  config.py             every knob: net classes, the 3W rule, severity weights
  parse_board.py        KiCad board -> nets and adjacency edges
  ingest.py             load the graph, embed the corpus, build the vector index
  prepare_autograph.py  generate the synthetic defect corpus for AutoGraph
  agent.py              the resolver: recall, traverse, score, judge, cite, corroborate
  agent_graph.py        the same investigation driven by a LangGraph ReAct agent
data/
  CM4IOUSB3.kicad_pcb   the board
  autograph/            the generated corpus (markdown + manifest)
assets/                 figures saved by the notebook
crosstalk_analysis.ipynb
```

Everything the engine turns lives in `config.py`. Change the board, the spacing rule, or the
severity weights there and the whole pipeline follows, with no edit to any query.

---

## Honest edges

The engine only catches faults that can be modeled as a relationship between nets; a defect that
is not an adjacency is invisible to it. The adjacency edges depend on a proximity threshold
someone chose. The top cause is a lead, not a verdict, capped at `corroborated` until confirmed
on silicon. The board is real geometry; the defect corpus is synthetic and labeled as such, so
the build demonstrates the method rather than real hardware history.

The same traversal extends to impedance mismatch, return-path discontinuity, and power-integrity
issues, because each is a relationship in the same graph. Any board with routed nets works
without touching the query; only the graph grows.