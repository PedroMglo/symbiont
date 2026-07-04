INSERT INTO agentic_raw_outputs (
                    id, task_id, trace_id, agent, sha256, preview, redacted,
                    artifact_ref, size_bytes, created_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
