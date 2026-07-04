SELECT stage_name, round(avg(latency_ms), 0) as avg_ms,
               round(max(latency_ms), 0) as max_ms,
               sum(items_in) as total_in, sum(items_out) as total_out
        FROM rag_ingest_stages
        WHERE timestamp > now() - INTERVAL {0} DAY AND stage_name != ''
        GROUP BY stage_name ORDER BY avg_ms DESC
