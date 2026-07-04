SELECT route_mode, count() as value
        FROM rag_retrieval
        WHERE timestamp > now() - INTERVAL {0} DAY AND route_mode != ''
        GROUP BY route_mode ORDER BY value DESC
