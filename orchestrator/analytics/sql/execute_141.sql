SELECT role, LENGTH(content) as content_length, created_at
            FROM sessions
            WHERE session_id = ?
            ORDER BY created_at
