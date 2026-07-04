SELECT
            toStartOfHour(timestamp) as time,
            countIf(cache_hit = 1) as hits,
            countIf(cache_hit = 0) as misses
        FROM rag_cag_operations
        WHERE timestamp > now() - INTERVAL {0} DAY AND event = 'cag_pack_get'
        GROUP BY time ORDER BY time
