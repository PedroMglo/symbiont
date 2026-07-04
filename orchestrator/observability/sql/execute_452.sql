SELECT hour, COUNT(*) as cnt
            FROM llm_call_log WHERE timestamp >= ?
            GROUP BY hour ORDER BY cnt DESC LIMIT 1
