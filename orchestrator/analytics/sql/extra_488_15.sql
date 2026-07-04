SELECT
                request_id, timestamp, model, backend, intent,
                total_tokens, total_latency_ms as latency_ms,
                stream, agentic, success, error_type
            FROM llm_events
            WHERE event IN ('request_completed', 'llm_call_completed')
            ORDER BY timestamp DESC
            LIMIT {0}
