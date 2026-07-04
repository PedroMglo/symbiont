INSERT INTO agentic_preapproval_windows (
                    id, task_id, action, scope_json, status, reason, created_by,
                    created_at, expires_at, max_uses, used_count, metadata_json,
                    revoked_at, revoked_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, NULL)
