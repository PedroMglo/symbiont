SELECT
                {0} as period,
                COUNT(*) as queries,
                COALESCE(SUM(total_tokens), 0) as tokens,
                COALESCE(AVG(latency_ms), 0) as avg_latency_ms,
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as errors
            FROM llm_call_log WHERE timestamp >= ?
            GROUP BY period ORDER BY period
