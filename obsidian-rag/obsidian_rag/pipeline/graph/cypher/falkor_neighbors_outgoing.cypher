MATCH (n:GraphNode {repo: $repo})-[r:GRAPH_EDGE]->(m:GraphNode {repo: $repo})
WHERE toLower(n.id) = toLower($node)
   OR toLower(n.label) CONTAINS toLower($node)
   OR toLower($node) CONTAINS toLower(n.label)
RETURN m.id, m.label, m.file_type, m.source_file, r.relation, r.confidence, 'outgoing', 1
LIMIT $limit
