SELECT
            countIf(event = 'cag_pack_get') as total_gets,
            countIf(event = 'cag_pack_get' AND cache_hit = 1) as hits,
            round(countIf(event = 'cag_pack_get' AND cache_hit = 1) * 100.0 /
                  greatest(countIf(event = 'cag_pack_get'), 1), 1) as hit_rate,
            countIf(event = 'cag_response_cache' AND cache_hit = 1) as response_hits,
            countIf(event = 'cag_response_cache') as response_total,
            round(avgIf(nodes_matched, event = 'graph_context_built'), 1) as avg_nodes,
            round(avgIf(communities_used, event = 'graph_context_built'), 1) as avg_communities
        FROM rag_cag_operations
        WHERE timestamp > now() - INTERVAL {0} DAY
