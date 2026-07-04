# Storage infrastructure

External storage is resolved by the central config resolver and written to `.env.storage.generated`.

Current external root:

```text
/mnt/ai-extreme/ai-local
```

Operational commands:

```bash
make infra
```

Helpers live in `infra/storage/scripts/`. They are not the main storage API;
`make infra` is the canonical lifecycle command for normal use.

Do not move or delete data, models, caches, Docker volumes or `.bak` directories as part of structural cleanup.
