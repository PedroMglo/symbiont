UNWIND $nodes AS row
MERGE (n:GraphNode {repo: $repo, id: row.id})
SET n.label = row.label,
    n.name = row.name,
    n.type = row.type,
    n.file_type = row.file_type,
    n.source_file = row.source_file,
    n.props_json = row.props_json
