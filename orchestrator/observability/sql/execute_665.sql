SELECT first_token_latency_ms FROM llm_call_log
            WHERE timestamp >= ? AND first_token_latency_ms IS NOT NULL
            ORDER BY first_token_latency_ms
