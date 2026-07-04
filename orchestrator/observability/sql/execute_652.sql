SELECT latency_ms FROM llm_call_log
            WHERE timestamp >= ? AND success = 1
            ORDER BY latency_ms
