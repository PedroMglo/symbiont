# Security Policy

## Supported Versions

This project is under active development. Security fixes are currently provided for the main development branch and for the latest tagged release, when releases exist.

| Version / Branch | Supported |
| ---------------- | --------- |
| `main`           | ✅ Yes |
| Latest tagged release | ✅ Yes |
| Older tags / old commits | ⚠️ Best effort only |
| Forks or modified deployments | ❌ Not officially supported |

## Project Security Scope

This repository contains a local AI system with agentic execution, Docker-based services, local LLM integration, RAG/CAG/graph components, storage management and automation tooling.

Security-sensitive areas include:

- agent execution and sandbox/runtime isolation;
- shell, code and file operations performed by agents;
- Docker, Compose and container configuration;
- API authentication, HTTPS and local network exposure;
- secret handling, `.env` files and credentials;
- storage access, path traversal and write permissions;
- RAG ingestion pipelines and document processing;
- prompt-injection cases that can cause unauthorized code execution, file access, data exfiltration or policy bypass;
- dependency vulnerabilities affecting runtime services.

## Reporting a Vulnerability

Please do **not** report security vulnerabilities through public GitHub Issues.

Preferred reporting method:

1. Use GitHub private vulnerability reporting, if available for this repository.
2. If private reporting is not available, contact the maintainer privately before disclosing details publicly.

When reporting a vulnerability, please include:

- a clear description of the issue;
- affected component, service, agent or file path;
- steps to reproduce;
- expected and actual behavior;
- possible impact;
- logs, screenshots or proof of concept, when safe to share;
- suggested fix, if known.

Please avoid including real secrets, private tokens, personal data or destructive payloads in the report.

## Response Expectations

I aim to respond to valid security reports as follows:

| Stage | Target |
| ----- | ------ |
| Initial acknowledgement | Within 7 days |
| Initial triage | Within 14 days |
| Fix plan or mitigation | Depends on severity and complexity |
| Public disclosure | After a fix or mitigation is available |

Critical vulnerabilities that allow unauthorized code execution, secret leakage, filesystem escape, container escape or remote access bypass will be prioritized.

## Security Requirements

Contributions should follow these rules:

- never commit secrets, tokens, passwords, private keys or real `.env` files;
- use `.env.example` for documented configuration;
- keep write access limited and explicit;
- prefer least-privilege containers and read-only mounts where possible;
- validate paths before file operations;
- avoid exposing local APIs to the network unless explicitly configured;
- sanitize untrusted inputs before using them in shell commands;
- do not allow LLM output to execute commands without policy checks;
- keep dependency updates and vulnerability scanning enabled where practical.

## Out of Scope

The following are generally out of scope unless they demonstrate a concrete security impact:

- vulnerabilities requiring full local machine compromise before exploitation;
- issues only affecting unsupported forks or heavily modified deployments;
- social engineering without a technical exploit path;
- prompt-injection examples that do not bypass permissions, access private data, execute code or change files;
- denial-of-service cases that require unrealistic local resource access.

## Disclosure Policy

Please allow reasonable time for investigation and remediation before public disclosure.

Coordinated disclosure is appreciated. Reports made in good faith will be handled respectfully.
