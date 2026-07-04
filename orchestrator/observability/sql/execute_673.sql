SELECT
                model,
                COUNT(*) as queries,
                AVG(latency_ms) as avg_ms,
                MIN(latency_ms) as min_ms,
                MAX(latency_ms) as max_ms
            FROM llm_call_log
            WHERE timestamp >= ? AND success = 1
            GROUP BY model ORDER BY avg_ms
