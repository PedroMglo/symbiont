MERGE (m:GraphImport {repo: $repo})
SET m.source_hash = $source_hash,
    m.node_count = $node_count,
    m.edge_count = $edge_count
