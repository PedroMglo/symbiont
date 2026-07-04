UNWIND $terms AS term
MATCH (n:GraphNode {repo: $repo})
WHERE toLower(n.id) CONTAINS term
   OR toLower(n.label) CONTAINS term
   OR toLower(n.type) CONTAINS term
   OR toLower(n.source_file) CONTAINS term
WITH DISTINCT n LIMIT $limit
OPTIONAL MATCH (n)-[r:GRAPH_EDGE]->(m:GraphNode {repo: $repo})
RETURN n.id, n.label, n.type, n.file_type, n.source_file,
       m.id, m.label, r.relation
