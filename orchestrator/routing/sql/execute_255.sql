SELECT request_id, timestamp, query, intent, complexity,
                    action, agents_executed, reasoning, total_latency_ms,
                    success, fallback_used, user_rating, critic_score
                FROM routing_decisions
                ORDER BY timestamp DESC LIMIT ?
