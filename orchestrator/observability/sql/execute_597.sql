SELECT model, COUNT(*) as cnt
                FROM llm_call_log WHERE timestamp >= ? AND intent = ?
                GROUP BY model
