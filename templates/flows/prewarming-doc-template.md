# <Prewarming Area Or Service Intent>

Status: <implemented | enabled-by-default | opt-in | draft | blocked>
Owner: `orchestrator/prewarming`
Last verified: <YYYY-MM-DD>
Applies to: `orchestrator/prewarming`, lifecycle manager, service catalog
Audience: developer, operator, maintainer

## Page Index

- [Purpose](#purpose)
- [Non-Ownership](#non-ownership)
- [Runtime Flow](#runtime-flow)
- [Service Intent Entry](#service-intent-entry)
- [Decision Diagram](#decision-diagram)
- [Resource And Safety Rules](#resource-and-safety-rules)
- [Metrics And Learning](#metrics-and-learning)
- [Failure Modes](#failure-modes)
- [Verification](#verification)
- [Open Questions](#open-questions)

## Purpose

Explain what this prewarming behavior predicts and which latency problem it
solves. Prewarming is best-effort runtime infrastructure: it starts likely
needed owners earlier while the normal request path continues.

## Non-Ownership

Prewarming must not own:

- Feature business logic.
- Agent prompt behavior.
- Direct-answer behavior.
- Storage lifecycle.
- Endpoint inference outside runtime registry/config.
- Scenario-specific prompt examples as routing truth.

## Runtime Flow

```mermaid
sequenceDiagram
    participant G as Gateway/request path
    participant E as Prewarming engine
    participant S as SignalExtractor
    participant R as Rule/Semantic Routers
    participant A as Aggregator
    participant P as PolicyEngine
    participant L as Lifecycle Manager
    participant D as Normal Dispatch

    G->>E: schedule best-effort prewarm task
    G->>D: continue normal request path
    E->>S: extract cheap request signals
    S-->>E: text, attachment, operation signals
    E->>R: score service intents
    R-->>E: candidate owners with confidence
    E->>A: merge scores, resource pressure, recency
    A->>P: apply thresholds and limits
    alt allowed
        P->>L: request lifecycle start
        L-->>E: started/already-running/failure
    else denied or low confidence
        P-->>E: skip with reason
    end
    D->>E: mark_used after real dispatch
```

## Service Intent Entry

| Field | Value | Notes |
| --- | --- | --- |
| `id` | `<service-id>` | Must map to live lifecycle service. |
| `description` | `<stable role>` | One stable sentence. |
| `capabilities` | `<capabilities>` | Domain-neutral. |
| `inputs` | `<input types>` | File/content/request types. |
| `operations` | `<operations>` | Verbs/tasks. |
| `keywords` | `<keywords>` | Cheap signals only. |
| `patterns` | `<patterns>` | Durable patterns, no benchmark shortcuts. |
| `file_extensions` | `<extensions>` | Only if truly relevant. |
| `prewarm_policy` | `<standard|aggressive|conservative|never>` | Include reason for `never`. |
| `prewarm_threshold` | `<number>` | Policy input. |
| `uses_gpu` | `<true|false>` | Resource policy input. |
| `ttl_idle` | `<duration>` | Cleanup behavior. |

## Decision Diagram

```mermaid
flowchart TD
    Request[Incoming request] --> Signals[Extract signals]
    Signals --> Direct{Likely direct answer\nand no service signal?}
    Direct -- yes --> Skip[Skip prewarming]
    Direct -- no --> Rules[RuleRouter]
    Rules --> Strong{High confidence?}
    Strong -- yes --> Policy[PolicyEngine]
    Strong -- no --> Semantic[SemanticRouterAdapter]
    Semantic --> Micro{Still ambiguous?}
    Micro -- yes --> Classifier[MicroClassifier]
    Micro -- no --> Aggregate[Aggregator]
    Classifier --> Aggregate
    Rules --> Aggregate
    Aggregate --> Policy
    Policy --> Allowed{Allowed by resource policy?}
    Allowed -- yes --> Start[Lifecycle start request]
    Allowed -- no --> SkipReason[Skip with reason]
```

## Resource And Safety Rules

| Rule | Reason | Expected behavior |
| --- | --- | --- |
| Best effort only | User request must continue | Failure is non-fatal. |
| Conservative GPU behavior | Avoid starving active work | Skip or delay GPU owners under pressure. |
| Live service mapping required | Avoid ghost catalog entries | Fix catalog/registry mismatch. |
| `example_queries` are non-authoritative | Avoid prompt overfitting | Use intent documents for semantic routing. |
| Direct answer is not a service here | Preserve owner boundary | `reasoning_and_response` owns direct answer behavior. |

## Metrics And Learning

| Signal | Meaning | Action |
| --- | --- | --- |
| prewarm requested | Candidate selected | Check confidence and policy. |
| prewarm started | Lifecycle accepted | Measure latency benefit. |
| mark_used hit | Prediction matched dispatch | Reinforce service intent. |
| mark_used miss | Prediction not used | Review keywords/patterns/threshold. |
| skipped by policy | Resource or threshold gate | Tune policy, not feature behavior. |

## Failure Modes

| Failure | User impact | Correct recovery |
| --- | --- | --- |
| Router error | None; request continues | Fix router/test, keep fail-open to main path. |
| Lifecycle start failed | Possible latency loss | Inspect lifecycle/service health. |
| Ghost service id | Prewarm cannot start real service | Fix catalog/registry contract. |
| False positive | Resource waste | Tune intent metadata and threshold. |
| False negative | No latency benefit | Add durable service intent signal. |

## Verification

| Check | Command or source | Expected result | Last run |
| --- | --- | --- | --- |
| Prewarming tests | `<command>` | pass | <date or not-run> |
| Catalog validity | `<command>` | all service ids live | <date or not-run> |
| Lifecycle integration | `<command>` | start/skip reason recorded | <date or not-run> |
| Runtime smoke | `<command>` | main request succeeds even if prewarm fails | <date or not-run> |

## Open Questions

- <question, owner, or decision still pending>
