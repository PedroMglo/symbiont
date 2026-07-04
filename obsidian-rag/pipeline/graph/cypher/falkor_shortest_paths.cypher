MATCH p = shortestPath((s:GraphNode {repo: $repo})-[:GRAPH_EDGE*..8]->(t:GraphNode {repo: $repo}))
WHERE (toLower(s.id) = toLower($source) OR toLower(s.label) CONTAINS toLower($source))
  AND (toLower(t.id) = toLower($target) OR toLower(t.label) CONTAINS toLower($target))
RETURN [node IN nodes(p) | node.label]
LIMIT $limit
