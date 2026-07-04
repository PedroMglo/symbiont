SELECT * FROM agentic_tasks
                WHERE status IN (?, ?)
                ORDER BY
                    CASE priority
                        WHEN 'high' THEN 0
                        WHEN 'normal' THEN 1
                        WHEN 'low' THEN 2
                        ELSE 3
                    END,
                    created_at
                LIMIT 200
