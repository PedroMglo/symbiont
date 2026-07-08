SELECT sources_used, count() as value
        FROM rag_retrieval
        WHERE timestamp > now() - INTERVAL {0} DAY AND sources_used != ''
        GROUP BY sources_used ORDER BY value DESC
