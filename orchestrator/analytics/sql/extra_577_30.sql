SELECT uniqExact(session_id) as cnt
                FROM llm_events WHERE session_id != ''
