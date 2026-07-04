# Agentic Readiness Status Spec

Owner: `orchestrator/agentic`.

Readiness status is an orchestrator-owned projection over runtime checks. It
does not replace owner health endpoints, service metrics or Resource Governor
policy. It only normalizes whether the agentic control plane is ready for
operator opt-in and explains the reason in ledger/cockpit views.

## Canonical Status Values

- `ready`: every required readiness check passed.
- `degraded`: the runtime can operate, but non-terminal evidence is missing or
  below the expected quality bar.
- `blocked`: one or more required checks failed and autonomous opt-in must not
  proceed.
- `stale`: replay or evidence freshness checks cannot prove current state from
  the ledger.
- `unexpected_down`: readiness could not produce a coherent status.

Existing `pass`, `warn` and `fail` check statuses remain for compatibility.
The canonical status is an additional summary field used by cockpit, eval and
operator views.
