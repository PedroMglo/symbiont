SELECT
                backend,
                COUNT(*) as queries,
                COALESCE(SUM(total_tokens), 0) as total_tokens,
                COALESCE(AVG(latency_ms), 0) as avg_latency_ms,
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as errors,
                SUM(CASE WHEN fallback_used = 1 THEN 1 ELSE 0 END) as fallback_to_count
            FROM llm_call_log WHERE timestamp >= ?
            GROUP BY backend ORDER BY queries DESC
