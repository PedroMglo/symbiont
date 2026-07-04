MATCH (n:GraphNode {repo: $repo})
DETACH DELETE n
