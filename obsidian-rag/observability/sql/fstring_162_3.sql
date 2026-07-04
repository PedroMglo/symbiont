SELECT
            {0}(timestamp) as time,
            count() as requests,
            countIf(success = 0) as errors,
            round(avg(latency_ms), 1) as avg_latency,
            round(quantile(0.95)(latency_ms), 1) as p95_latency
        FROM rag_requests
        WHERE timestamp > now() - INTERVAL {1} DAY
        GROUP BY time
        ORDER BY time
