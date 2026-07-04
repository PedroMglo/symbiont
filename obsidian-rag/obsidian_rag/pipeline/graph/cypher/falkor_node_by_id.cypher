MATCH (n:GraphNode {repo: $repo, id: $node_id})
RETURN n.id, n.label, n.type, n.file_type, n.source_file
LIMIT 1
