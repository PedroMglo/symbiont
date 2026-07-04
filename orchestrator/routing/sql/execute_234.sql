SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successes,
                    SUM(CASE WHEN fallback_used = 1 THEN 1 ELSE 0 END) as fallbacks,
                    AVG(total_latency_ms) as avg_latency_ms,
                    AVG(user_rating) as avg_rating,
                    COUNT(user_rating) as rated_count
                FROM routing_decisions WHERE timestamp > ?
