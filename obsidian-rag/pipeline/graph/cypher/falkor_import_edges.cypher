UNWIND $edges AS row
MATCH (a:GraphNode {repo: $repo, id: row.source})
MATCH (b:GraphNode {repo: $repo, id: row.target})
MERGE (a)-[r:GRAPH_EDGE {repo: $repo, edge_id: row.edge_id}]->(b)
SET r.relation = row.relation,
    r.confidence = row.confidence,
    r.props_json = row.props_json
