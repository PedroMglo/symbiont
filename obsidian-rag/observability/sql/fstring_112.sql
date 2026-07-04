SELECT
            count() as total_requests,
            countIf(success = 0) as errors,
            round(countIf(success = 0) * 100.0 / greatest(count(), 1), 2) as error_rate,
            round(avg(latency_ms), 1) as avg_latency,
            round(quantile(0.95)(latency_ms), 1) as p95_latency
        FROM rag_requests
        WHERE timestamp > now() - INTERVAL {0} DAY
