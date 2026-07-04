SELECT backend, COUNT(*) as cnt
            FROM llm_call_log WHERE timestamp >= ?
            GROUP BY backend ORDER BY cnt DESC LIMIT 1
