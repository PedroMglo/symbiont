INSERT INTO agentic_approvals (
                    id, task_id, action, risk_level, payload_preview, payload_hash,
                    dry_run_result, status, requested_at, expires_at, approved_by,
                    approved_at, rejected_reason, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
