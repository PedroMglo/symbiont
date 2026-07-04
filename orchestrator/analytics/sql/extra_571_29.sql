SELECT uniqExact(session_id) as cnt
                FROM llm_events
                WHERE timestamp >= '{0}' AND session_id != ''
