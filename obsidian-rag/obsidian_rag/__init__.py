"""Obsidian RAG — Local RAG pipeline for Obsidian Vault."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("obsidian-rag")
except PackageNotFoundError:
    __version__ = "0+local"
