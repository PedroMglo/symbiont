SELECT *
            FROM llm_events
            WHERE timestamp >= '{0}' AND event IN ('request_completed', 'llm_call_completed')
            ORDER BY timestamp
