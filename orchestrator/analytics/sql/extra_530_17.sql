SELECT
                count() as request_count,
                sum(total_tokens) as sum_tok,
                sum(prompt_tokens) as sum_prompt,
                sum(completion_tokens) as sum_compl,
                round(avg(total_latency_ms), 1) as avg_lat,
                groupUniqArray(model) as models_used,
                groupUniqArray(backend) as backends_used,
                countIf(fallback_used = 1) as fallbacks,
                countIf(success = 0) as errors,
                countIf(agentic = 1) as agentic_calls,
                countIf(stream = 1) as stream_calls
            FROM llm_events
            WHERE session_id = '{0}' AND event IN ('request_completed', 'llm_call_completed')
