import os, random, sys
from dotenv import load_dotenv
from arango import ArangoClient
sys.path.insert(0, "src")
import agent, config as C

load_dotenv()
db = ArangoClient(hosts=os.environ["ARANGO_HOST"]).db(
    os.environ["ARANGO_DB"],
    username=os.environ["ARANGO_USER"],
    password=os.environ["ARANGO_PASSWORD"],
)

# find the hero victim key by name
rows = list(db.aql.execute(
    'FOR n IN nets FILTER n.name LIKE "%DSI1_D0_N%" RETURN n._key'))
victim = rows[0] if rows else "net_1"
print("victim key:", victim)

vec = [random.gauss(0, 1) for _ in range(1536)]   # dummy vector, just tests the AQL shape
similar, cands = agent._run_marquee(db, vec, victim, k=3)
print("similar returned:", len(similar), "| candidates returned:", len(cands))