SELECT uniqExact(session_id) as cnt
            FROM llm_events
            WHERE timestamp >= now() - INTERVAL {0} SECOND
              AND session_id != ''
