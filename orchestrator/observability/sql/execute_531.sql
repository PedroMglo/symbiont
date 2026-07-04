SELECT intent, COUNT(*) as cnt
                FROM llm_call_log WHERE timestamp >= ? AND model = ?
                GROUP BY intent
