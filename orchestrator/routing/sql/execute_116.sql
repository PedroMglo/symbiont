INSERT OR IGNORE INTO routing_decisions (
                    request_id, session_id, timestamp, query, intent, complexity,
                    action, reasoning, agents_planned, agents_executed,
                    routing_model, routing_latency_ms, execution_latency_ms,
                    synthesis_latency_ms, total_latency_ms, total_tokens,
                    success, fallback_used, critic_score, critic_acceptable,
                    response_preview, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
