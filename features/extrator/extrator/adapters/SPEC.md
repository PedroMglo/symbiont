# Parser Adapter Registry Spec

`extrator.adapters` owns document parser selection for the Extrator feature.
The registry selects one parser for a container-visible local file and returns a
single `NormalizedDocument`.

## Contract

- `parse_file(path, table_dir=...)` is the public parser entrypoint for the
  extraction pipeline.
- Parser choice is ranked by `[parsers] parser_priorities`.
- A parser may decline work by raising `AdapterUnavailable`.
- The registry must try the next eligible parser when an optional dependency is
  unavailable.
- Parser scoring fields are local evidence only: `confidence`, `tables`,
  `images`, `layout`, `warnings`, and `cost`.
- Parser fallback must be explicit in `NormalizedDocument.metadata` under
  `parser_selection`; callers must be able to see candidate order, attempts,
  selected parser, and unavailable reasons.

## Boundaries

- Adapters extract text, metadata and tables only.
- The pipeline owns manifest writes, chunking and result publication.
- `storage_guardian` owns durable object lifecycle.
- `obsidian-rag` owns embeddings and retrieval.

## Legacy Removal Rule

Do not add new extension switches to `adapters/__init__.py`. New parsers must be
registered in `registry.py`, tested against a golden fixture, and then any
dominated bespoke branch can be removed in the same milestone.
