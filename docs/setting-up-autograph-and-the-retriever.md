# Setting Up AutoGraph and the Retriever for Cited Fixes

This section sets up the citation side of the tutorial. It builds two things on the Arango
platform:

- a **knowledge graph**, which AutoGraph builds from the 30 past defect reports, and
- a **GraphRAG Retriever**, a service that answers questions from that knowledge graph and cites
  the reports it used. `src/agent.py` calls it to back the diagnosed cause with a cited fix.
Run it after `python src/prepare_autograph.py` (which writes the defect reports) and
`python src/ingest.py` (which loads the board graph and defect vectors into the database named by
`ARANGO_DB` in your `.env`). Ingest does not create the database, so create it first in the web
interface if it does not exist.

Without these services the agent still runs end to end; `cite_fix_via_retriever()` prints
`retriever not configured` and falls back to a local corpus citation. This section is what turns
that fallback into a cited answer from the knowledge graph.

> **Environment:** Arango Contextual Data Platform deployment at `https://your-deployment.arango.ai`
> (gateway on port `8529`). Authenticates with an Arango user account; a JWT is obtained via
> `POST /_open/auth`.

Throughout, `$EP` is the external endpoint, the same host as `ARANGO_HOST` in your `.env`:

```bash
EP="https://your-deployment.arango.ai"
```

Steps tagged **[CLI]** are scriptable. Steps tagged **[UI ONLY]** have no command-line path on the
current platform version (see [Is there a command-line way to deploy these services?](#is-there-a-command-line-way-to-deploy-these-services)).

---

## Step 1: Get a JWT [CLI]

The platform API authenticates with a standard Arango **user** JWT, the same one the agent mints in
`_get_jwt()`. Use the `ARANGO_USER` / `ARANGO_PASSWORD` from your `.env`.

```bash
TOKEN=$(curl -s -X POST "$EP/_open/auth" \
  -H "Content-Type: application/json" \
  -d '{"username": "<ARANGO_USER>", "password": "<ARANGO_PASSWORD>"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["jwt"])')

echo "$TOKEN" | cut -c1-20   # sanity check: prints the first characters of a JWT
```

**Success signal:** the response body is `{"jwt":"eyJ..."}`. Keep `TOKEN` set for every later call.

> If your deployment uses a self-signed certificate, add `-k` to every `curl`.

Docs: [Control Plane (ACP): obtaining a Bearer token](https://docs.arango.ai/platform-suite/control-plane-acp/) · [Arango JWT user tokens](https://docs.arango.ai/arangodb/stable/develop/http-api/authentication/#jwt-user-tokens)

---

## Step 2: Confirm control-plane health [CLI]

```bash
curl -s -X GET "$EP/_platform/acp/v1/health" \
  -H "Authorization: Bearer $TOKEN"
```

**Success signal:** `{"status":"OK"}`. The endpoint needs a valid token, so an empty or expired
token fails here first.

Docs: [Control Plane (ACP): health check](https://docs.arango.ai/platform-suite/control-plane-acp/)

---

## Step 3: Inspect what is already deployed [CLI]

List every installed service. An empty body (`{}`) returns everything.

```bash
curl -s -X POST "$EP/_platform/acp/v1/list_services" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
```

**Success signal:** a JSON object with a `services` array. Before any deploy it may be empty or hold
only base services. You will run this again in Step 6 to read back the services the wizard creates.

Docs: [Control Plane (ACP): listing services](https://docs.arango.ai/platform-suite/control-plane-acp/)

---

## Step 4: Build the AutoGraph project in the wizard [UI ONLY]

AutoGraph is provisioned through a three-step wizard: **Documents → Configure → Build**. It deploys
the AutoGraph service and builds the Corpus Graph in one flow, then lands on the project overview.

Open the web interface (`https://your-deployment.arango.ai/ui/`), **select your `ARANGO_DB`
database first** in the left sidebar (the project's collections are created inside the selected
database), then open **Agentic AI Suite → AutoGraph Studio** and create a new project.

### 4a: Documents

1. Click **Upload files** (or drag and drop) and select the 30 reports
   `data/autograph/dr_0001.md` … `data/autograph/dr_0030.md`.
2. **Do not upload `data/autograph/manifest.json`.** It holds each report's role and is for local
   use only; if AutoGraph reads it, the knowledge graph learns the answer labels.
3. Name the category `defect_reports`.

**Success signal:** the file list shows 30 documents under `defect_reports`, each marked
**Pending**. Click **Configure LLM**.

### 4b: Configure

These settings are fixed for this build; changing the provider or model later needs a rebuild.

1. **Provider** → **OpenAI**.
2. **Model** → `gpt-5-mini`, the same model as `CHAT_MODEL` in `src/config.py` (the agent's code
   does not depend on this choice).
3. **API key** → paste your key or pick a stored secret. Leave **Use a separate key for
   embeddings** unchecked to use one key for both.
4. **Embedding model** → `text-embedding-3-small` (`EMBEDDING_MODEL` in `src/config.py`).
5. **Multimodal model** → provider default.
6. Click **Start build**.

**Success signal:** the panel confirms it is building from 30 documents across 1 category
(`defect_reports`).

### 4c: Build

1. The **Build** step shows **AutoGraph service deployed** with its service ID (for example
   `arangodb-autograph-xxxxx`).
2. Click **Build Corpus Graph**.

**Success signal:** the wizard finishes at the project overview. The **Context Graph** card shows
the **Corpus Graph** (30 documents) with **Open in Graph Visualizer**, and the **Knowledge Graph**
marked "Not built yet."

Docs: [AutoGraph web interface](https://docs.arango.ai/agentic-ai-suite/autograph/)

---

## Step 5: Generate strategies, build the Knowledge Graph, deploy the Retriever [UI ONLY]

### 5a: Knowledge Graph

1. On the **Knowledge Graph** card, click **Generate strategies**.
2. **Configure strategy generation:** the complexity slider sets the starting GraphRAG / VectorRAG
   mix (**Vector only** ↔ **Balanced graph** ↔ **Graph + images**). Higher complexity means richer
   entity extraction and higher cost. This corpus is short text with no images, so a balanced or
   graph-leaning setting is enough. Click **Generate strategies**.
3. **Review strategies:** each cluster shows its RAG strategy and generated ontology. Check that the
   defect-report cluster uses **GraphRAG**; the cited fix relies on entities such as mechanism,
   aggressor type and remedy, which a VectorRAG cluster does not extract. Click **Continue to build**.
4. **Build:** leave **Parallel builds** at `1` and click **Build Knowledge Graph**. When the
   **Knowledge Graph built** confirmation appears, click **Go to overview**.

**Success signal:** the **Knowledge Graph** card shows entity and relationship counts and an
**Open in Graph Visualizer** link. Exact counts vary with the complexity setting and model version.

### 5b: Retriever

1. In **Start using AutoRAG**, click **Deploy a retriever**.
2. Choose the chat provider (**OpenAI**), chat model (`gpt-5-mini`) and chat API key. The embedding
   provider and model are locked to match the corpus build, so query embeddings line up with the
   ones used at import.
3. Deploy.

**Success signal:** the retriever appears on the project overview. Its service ID has the form
`arangodb-graphrag-retriever-xxxxx`; Step 6 reads it back from the command line.

Docs: [AutoGraph web interface](https://docs.arango.ai/agentic-ai-suite/autograph/) · [GraphRAG Retriever](https://docs.arango.ai/agentic-ai-suite/graphrag/)

---

## Step 6: Read back the Retriever ID and set `.env` [CLI]

```bash
curl -s -X POST "$EP/_platform/acp/v1/list_services" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}' | python3 -m json.tool
```

`list_services` returns a `services` array with every service on the deployment, including
those of other projects and databases. Each entry carries its fields directly (there is no
`serviceInfo` wrapper on this endpoint); retrievers also carry a `serviceMeta` block:

```json
{
  "services": [
    { "serviceId": "arangodb-autograph-xxxxx", "status": "DEPLOYED",
      "dbName": "<ARANGO_DB>", "genaiProjectName": "<your-project-name>", "...": "..." },
    { "serviceId": "arangodb-graphrag-retriever-xxxxx", "status": "DEPLOYED",
      "dbName": "<ARANGO_DB>", "genaiProjectName": "<your-project-name>",
      "serviceMeta": { "serviceType": "arangodb-graphrag-retriever", "retrieverMeta": { "...": "..." } } }
  ]
}
```

Extract the Retriever's ID. A deployment can hold several retrievers, so match on the database
as well (replace `<ARANGO_DB>` with the value in your `.env`):

```bash
RETRIEVER_ID=$(curl -s -X POST "$EP/_platform/acp/v1/list_services" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{}' \
  | python3 -c 'import sys,json
d=json.load(sys.stdin)
print([s["serviceId"] for s in d["services"] if "retriever" in s["serviceId"] and s.get("dbName")=="<ARANGO_DB>"][0])')

echo "$RETRIEVER_ID"            # e.g. arangodb-graphrag-retriever-xxxxx
```

### 6a: Set `.env`

```
RETRIEVER_ID=arangodb-graphrag-retriever-xxxxx
RETRIEVER_BASE=https://your-deployment.arango.ai
```

`RETRIEVER_BASE` is the deployment host, usually the same as `ARANGO_HOST`; leave the port off and
the agent adds `:8529`. The agent derives the **postfix**, the last hyphen-separated segment of
`RETRIEVER_ID` (`xxxxx`), and builds the query URL from it:

```
$EP:8529/graphrag/retriever/<postfix>/v1/graphrag-query
```

### 6b: Confirm the service status

This endpoint **does** wrap its response in `serviceInfo`:

```bash
curl -s -X GET "$EP/_platform/acp/v1/service/$RETRIEVER_ID" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "serviceInfo": {
    "serviceId": "arangodb-graphrag-retriever-xxxxx",
    "description": "Install complete",
    "status": "DEPLOYED",
    "dbName": "<ARANGO_DB>",
    "genaiProjectName": "<your-project-name>"
  }
}
```

**Success signal:** `status: "DEPLOYED"` and `dbName` matches your `ARANGO_DB`. `DEPLOYED` means installed;
it can take a moment longer to accept queries.

> `$EP`, `$TOKEN` and `$RETRIEVER_ID` do not carry across terminal windows. In a new shell, set
> `$EP` and repeat Step 1 first, or the call returns an empty body and `json.tool` fails with
> `Expecting value: line 1 column 1`.

Docs: [Control Plane (ACP): listing services, service status](https://docs.arango.ai/platform-suite/control-plane-acp/)

---

## Is there a command-line way to deploy these services?

There **is** an official Arango command-line tool, but it does **not** wrap the per-service install
API, so it cannot replace the wizard in Steps 4 and 5.

The `arangodb_operator_platform` tool (the "Platform CLI", from the
[kube-arangodb releases](https://github.com/arangodb/kube-arangodb/releases)) manages the cluster
and operator lifecycle at the Kubernetes and Helm layer (licensing, packaging, and installing the
Platform Suite). It does not touch AutoGraph or Retriever instances. Per-project AI services are deployed through
the platform, by the web interface wizard on the current version. `oasisctl` manages ArangoGraph
Cloud deployments and does not apply here either.

Docs: [CDP install and upgrade](https://docs.arango.ai/contextual-data-platform/install-and-upgrade/) · [Offline setup: `arangodb_operator_platform` commands](https://docs.arango.ai/contextual-data-platform/install-and-upgrade/offline-setup/)

---

## Quick reference

| Action | Method + path | Tag |
| --- | --- | --- |
| Get JWT | `POST /_open/auth` | CLI |
| Control-plane health | `GET /_platform/acp/v1/health` | CLI |
| List services | `POST /_platform/acp/v1/list_services` (body `{}`) | CLI |
| One service's status | `GET /_platform/acp/v1/service/{serviceId}` | CLI |
| Create project, deploy AutoGraph | UI: Agentic AI Suite → AutoGraph Studio → Documents → Configure → Build | UI ONLY |
| Build Corpus Graph | UI: Build step → Build Corpus Graph | UI ONLY |
| Strategies + Knowledge Graph | UI: Generate strategies → Review → Continue to build → Build Knowledge Graph | UI ONLY |
| Deploy Retriever | UI: Start using AutoRAG → Deploy a retriever | UI ONLY |