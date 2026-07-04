SELECT
                    COUNT(*) as request_count,
                    COALESCE(SUM(total_tokens), 0) as total_tokens,
                    COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) as completion_tokens,
                    COALESCE(AVG(latency_ms), 0) as avg_latency_ms,
                    GROUP_CONCAT(DISTINCT model) as models_used,
                    GROUP_CONCAT(DISTINCT backend) as backends_used,
                    SUM(CASE WHEN fallback_used = 1 THEN 1 ELSE 0 END) as fallbacks,
                    SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as errors,
                    SUM(CASE WHEN agentic = 1 THEN 1 ELSE 0 END) as agentic_calls,
                    SUM(CASE WHEN rag_used = 1 THEN 1 ELSE 0 END) as rag_calls,
                    SUM(CASE WHEN stream = 1 THEN 1 ELSE 0 END) as stream_calls
                FROM llm_call_log WHERE session_id = ?
