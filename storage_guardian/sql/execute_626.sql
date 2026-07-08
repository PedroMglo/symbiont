SELECT * FROM storage_upload_sessions
                WHERE status = 'uploading' AND expires_at < ?
                ORDER BY expires_at ASC
