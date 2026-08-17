# Security policy

Do not report vulnerabilities in public issues. GitHub private vulnerability
reporting is unavailable for this private personal-account repository. The
exact private fallback channel for `PedroMglo/ai-local-stack` is email to
`pedro.lourenco2001@hotmail.com` with subject `[SECURITY] PedroMglo/ai-local-stack`. Send an
initial impact summary without credentials, exploit payloads or sensitive
runtime evidence; agree an encrypted transfer channel with the owner before
sending sensitive reproduction material.

Every pull request and push to `main` must pass the repository-local security
workflow. The portable workflow and hooks enforce secret detection, private-key
checks, Bandit, Ruff and actionlint without GitHub Code Security. Signed commits,
linear history, reviewed protected-branch changes, Dependabot alerts and
Dependabot security updates remain remote settings to apply after the first
push. Risk acceptance must be exact, time-bounded, owner-approved and stored in
the repository that owns the affected code.
