SELECT
            toStartOfHour(timestamp) as time,
            round(avgIf(latency_ms, event = 'store_query'), 1) as query_ms,
            round(avgIf(latency_ms, event = 'store_upsert'), 1) as upsert_ms
        FROM rag_store_operations
        WHERE timestamp > now() - INTERVAL {0} HOUR
        GROUP BY time ORDER BY time
