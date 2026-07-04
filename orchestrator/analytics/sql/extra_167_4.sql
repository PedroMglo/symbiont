SELECT toHour(timestamp) as hr, count() as cnt
            FROM llm_events
            WHERE timestamp >= '{0}' AND event IN ('request_completed', 'llm_call_completed')
            GROUP BY hr ORDER BY cnt DESC LIMIT 1
