SELECT query, intent, complexity, action, agents_executed, reasoning
                FROM routing_decisions
                WHERE timestamp > ?
                  AND (
                    user_rating >= ?
                    OR (success = 1 AND critic_acceptable = 1 AND user_rating IS NULL)
                  )
                ORDER BY
                    CASE WHEN user_rating IS NOT NULL THEN 0 ELSE 1 END,
                    user_rating DESC,
                    timestamp DESC
                LIMIT ?
