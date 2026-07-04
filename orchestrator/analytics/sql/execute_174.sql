SELECT
                DATE(created_at, 'unixepoch') as date,
                COUNT(DISTINCT session_id) as sessions,
                COUNT(*) as messages,
                SUM(CASE WHEN role = 'user' THEN 1 ELSE 0 END) as user_messages
            FROM sessions
            WHERE created_at >= ?
            GROUP BY date
            ORDER BY date
