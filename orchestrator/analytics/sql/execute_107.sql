SELECT
                session_id,
                COUNT(*) as message_count,
                SUM(CASE WHEN role = 'user' THEN 1 ELSE 0 END) as user_messages,
                SUM(CASE WHEN role = 'assistant' THEN 1 ELSE 0 END) as assistant_messages,
                MIN(created_at) as started_at,
                MAX(created_at) as last_activity_at,
                SUM(LENGTH(content)) as total_content_length
            FROM sessions
            WHERE created_at >= ?
            GROUP BY session_id
            ORDER BY last_activity_at DESC
            LIMIT ? OFFSET ?
