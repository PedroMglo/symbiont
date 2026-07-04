# Capability Schema Reference Spec

Owner: `orchestrator/capabilities`.

Capability manifests describe public runtime contracts. Their `input_schema`
and `output_schema` entries must include a resolvable `schema_ref`; free-text
`contract` names are optional labels only and are not accepted as the source of
truth.

## Format

```toml
input_schema = {
  type = "object",
  contract = "DisplayName",
  schema_ref = "python:package.module:ModelName",
  required = ["field"]
}
```

Supported schemes:

- `python:<module>:<symbol>`: imports a Pydantic model, dataclass, or other
  type accepted by `pydantic.TypeAdapter` and renders JSON Schema.

## Rules

- Schema refs are validation metadata, not execution handles.
- The orchestrator may resolve schema refs in tests, CI, and low-cost
  validation gates.
- Runtime tool envelopes must expose the input/output refs as routing metadata
  without resolving owner packages during normal dispatch.
- Runtime dispatch must still cross API/dispatch boundaries and must not import
  owner packages to execute behavior.
- `required` and `fields` in manifests must be subsets of the resolved JSON
  Schema when they are declared.
- Owners publish or expose the referenced contracts. The orchestrator validates
  resolvability; it does not copy owner model definitions.

## Schema Reference Requirement

Manifest schema entries must use `schema_ref`. A `contract` string by itself is
documentation, not a contract.
