SELECT
                count() as total_queries,
                sum(total_tokens) as sum_tokens,
                sum(prompt_tokens) as sum_prompt_tokens,
                sum(completion_tokens) as sum_completion_tokens,
                round(avg(total_tokens)) as avg_tpq,
                round(avg(total_latency_ms), 1) as avg_lat_ms,
                uniqExact(session_id) as unique_sessions,
                countIf(agentic = 1) as agentic_queries,
                countIf(fallback_used = 1) as fallback_count,
                countIf(success = 0) as error_count,
                countIf(stream = 1) as stream_queries
            FROM llm_events
            WHERE timestamp >= '{0}'
              AND event IN ('request_completed', 'llm_call_completed')
