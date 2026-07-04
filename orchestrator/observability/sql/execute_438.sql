SELECT model, COUNT(*) as cnt, COALESCE(SUM(total_tokens), 0) as tokens
            FROM llm_call_log WHERE timestamp >= ?
            GROUP BY model ORDER BY cnt DESC LIMIT 1
