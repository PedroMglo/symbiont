SELECT
                {0} as period,
                count() as queries,
                sum(total_tokens) as sum_tok,
                round(avg(total_latency_ms), 1) as avg_lat,
                countIf(success = 0) as errors
            FROM llm_events
            WHERE timestamp >= '{1}' AND event IN ('request_completed', 'llm_call_completed')
            GROUP BY period ORDER BY period
