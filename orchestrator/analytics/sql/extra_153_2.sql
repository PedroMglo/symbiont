SELECT model, count() as cnt, sum(total_tokens) as tok
            FROM llm_events
            WHERE timestamp >= '{0}' AND event IN ('request_completed', 'llm_call_completed')
            GROUP BY model ORDER BY cnt DESC LIMIT 1
