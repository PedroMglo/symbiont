"""Parser and converter adapters used by the extrator pipeline."""

from extrator.adapters.registry import ParserAdapter, ParserEvidence, parse_file, parser_candidates

__all__ = ["ParserAdapter", "ParserEvidence", "parse_file", "parser_candidates"]
