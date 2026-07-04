SELECT gate_reason, count() as value
        FROM rag_retrieval
        WHERE timestamp > now() - INTERVAL {0} DAY AND gate_passed = 0 AND gate_reason != ''
        GROUP BY gate_reason ORDER BY value DESC LIMIT 10
