SELECT pack_type, count() as value
        FROM rag_cag_operations
        WHERE timestamp > now() - INTERVAL {0} DAY AND pack_type != ''
        GROUP BY pack_type ORDER BY value DESC
