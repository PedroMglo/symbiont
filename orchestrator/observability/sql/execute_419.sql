SELECT
                COUNT(*) as total_queries,
                COALESCE(SUM(total_tokens), 0) as total_tokens,
                COALESCE(SUM(prompt_tokens), 0) as total_prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) as total_completion_tokens,
                COALESCE(AVG(total_tokens), 0) as avg_tokens_per_query,
                COALESCE(AVG(latency_ms), 0) as avg_latency_ms,
                COUNT(DISTINCT session_id) as unique_sessions,
                SUM(CASE WHEN agentic = 1 THEN 1 ELSE 0 END) as agentic_queries,
                SUM(CASE WHEN fallback_used = 1 THEN 1 ELSE 0 END) as fallback_count,
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as error_count,
                SUM(CASE WHEN stream = 1 THEN 1 ELSE 0 END) as stream_queries
            FROM llm_call_log WHERE timestamp >= ?
