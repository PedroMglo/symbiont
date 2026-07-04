SELECT
                backend,
                count() as queries,
                sum(total_tokens) as sum_tok,
                round(avg(total_latency_ms), 1) as avg_lat,
                countIf(success = 0) as errors,
                countIf(fallback_used = 1) as fallback_to_count
            FROM llm_events
            WHERE timestamp >= '{0}' AND event IN ('request_completed', 'llm_call_completed')
            GROUP BY backend ORDER BY queries DESC
