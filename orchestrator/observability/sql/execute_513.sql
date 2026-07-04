SELECT
                model,
                COUNT(*) as queries,
                COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) as completion_tokens,
                COALESCE(SUM(total_tokens), 0) as total_tokens,
                COALESCE(AVG(latency_ms), 0) as avg_latency_ms,
                COUNT(DISTINCT session_id) as sessions,
                SUM(CASE WHEN agentic = 1 THEN 1 ELSE 0 END) as agentic_count,
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as errors
            FROM llm_call_log WHERE timestamp >= ?
            GROUP BY model ORDER BY queries DESC
