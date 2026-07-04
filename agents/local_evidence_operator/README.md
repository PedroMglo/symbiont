# Local Evidence Operator

`local_evidence_operator` is the consolidated owner for read-only local
evidence that used to be split across narrow feature services.

It owns provider contracts for:

- code/repo/git evidence;
- data/schema/SQLite evidence;
- operational log/incident/Compose evidence;
- local security/redaction evidence.

This agent does not execute commands, mutate inputs, write final artifacts,
publish storage objects, or make policy decisions. It returns structured
evidence and declarative validation plans. Any execution goes through
`workspace_execution`; durable publication goes through `storage_guardian`.

## Provider Paths

The public provider paths remain stable during cutover:

- `/v1/code/*`
- `/v1/data/*`
- `/v1/ops/*`
- `/v1/security/*`

Those paths are now served by one owner and one service package. The previous
narrow feature owners were folded into this agent and are not runtime services.
