SELECT event, count() as value
        FROM rag_cag_operations
        WHERE timestamp > now() - INTERVAL {0} DAY
        GROUP BY event ORDER BY value DESC
