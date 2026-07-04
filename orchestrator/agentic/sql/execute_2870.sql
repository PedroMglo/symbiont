INSERT INTO agentic_actuations (
                    id, proposal_id, task_id, action, mode, status, before_json,
                    operation_json, after_json, impact_json, metadata_json,
                    error_json, created_at, updated_at, expires_at,
                    rolled_back_at, rollback_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, ?, ?, ?, NULL, NULL)
