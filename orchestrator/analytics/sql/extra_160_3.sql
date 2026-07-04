SELECT backend, count() as cnt
            FROM llm_events
            WHERE timestamp >= '{0}' AND event IN ('request_completed', 'llm_call_completed')
            GROUP BY backend ORDER BY cnt DESC LIMIT 1
