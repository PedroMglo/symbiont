MATCH (n:GraphNode {repo: $repo})
WHERE (n.source_file = $source_file
       OR $source_file ENDS WITH n.source_file
       OR n.source_file ENDS WITH $source_file)
  AND (toLower(n.label) = toLower($section_header)
       OR toLower(n.label) CONTAINS toLower($section_header)
       OR toLower($section_header) CONTAINS toLower(n.label))
RETURN n.id, n.label, n.type, n.file_type, n.source_file
LIMIT 1
