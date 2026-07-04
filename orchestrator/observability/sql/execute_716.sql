SELECT request_id, timestamp, model, backend, intent,
                   total_tokens, latency_ms, stream, agentic, success, error_type
            FROM llm_call_log
            ORDER BY timestamp DESC LIMIT ?
