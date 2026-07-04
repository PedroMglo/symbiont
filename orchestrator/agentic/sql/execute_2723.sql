INSERT INTO agentic_improvement_proposals (
                    id, task_id, kind, title, status, risk_level, confidence,
                    score, fingerprint, payload_json, evidence_json, metadata_json,
                    created_at, updated_at, expires_at, approval_id, applied_at,
                    rejected_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
