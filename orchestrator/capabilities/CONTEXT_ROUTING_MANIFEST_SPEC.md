# Context Routing Manifest Spec

Owner: `orchestrator/capabilities`.

Context routing manifests declare the default context sources selected for each
classified intent. They are routing metadata only: the orchestrator may choose
sources, but feature behavior and source-specific execution remain behind the
existing dispatch/API boundaries.

## Inputs

- `orchestrator/capabilities/context_routing.toml`
- `orchestrator.types.Intent`

## Rules

- Every current `Intent` value must have exactly one manifest entry.
- `sources` are ordered dispatch source names. They must match the configured
  source names consumed by `FeatureClient.query_source`.
- `required_sources` is the subset used by coverage/readiness checks. It must
  be a subset of `sources`.
- The router must consume this manifest through `ContextRoutingManifest`; it
  must not keep a parallel Python intent-to-source table.
- Speculative routing may reuse the same manifest API, but must not import
  private router state.

## Boundary

The manifest does not define owner service semantics, endpoint payloads, model
routing, or fallback behavior. Endpoint dispatch remains in generated config
and feature/service capability manifests.
