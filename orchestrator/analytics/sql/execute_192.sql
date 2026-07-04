SELECT
                DATE(created_at, 'unixepoch') || ' ' ||
                printf('%02d', CAST(strftime('%H', created_at, 'unixepoch') AS INTEGER)) || ':00' as period,
                COUNT(DISTINCT session_id) as sessions,
                COUNT(*) as messages
            FROM sessions
            WHERE created_at >= ?
            GROUP BY period
            ORDER BY period
