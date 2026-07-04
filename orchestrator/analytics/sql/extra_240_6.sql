SELECT
                model,
                count() as queries,
                sum(prompt_tokens) as sum_prompt,
                sum(completion_tokens) as sum_compl,
                sum(total_tokens) as sum_tok,
                round(avg(total_latency_ms), 1) as avg_lat,
                uniqExact(session_id) as sessions,
                countIf(agentic = 1) as agentic_count,
                countIf(success = 0) as errors
            FROM llm_events
            WHERE timestamp >= '{0}' AND event IN ('request_completed', 'llm_call_completed')
            GROUP BY model ORDER BY queries DESC
