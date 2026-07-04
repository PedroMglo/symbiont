INSERT INTO agentic_resource_leases (
                    id, task_id, lease_id, capability, decision, status,
                    acquired_at, renewed_at, released_at, expires_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
