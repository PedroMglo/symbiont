# ai-local HTTPS Gateway

This gateway is the project-level API entrypoint for local and LAN-safe
deployments. It uses Caddy with an internal local CA by default and keeps the
service containers behind the Docker network.

## Run

Start the core stack first so the `ai-local-net` network exists:

```bash
make infra
docker compose -f infra/gateway/compose.gateway.yml --profile gateway up -d
```

Default URLs:

- HTTPS: `https://ai-local.localhost:8443`
- Symbiont: `/`, `/query`, `/v1/*`
- RAG: `/rag/*`
- Qdrant admin: `/qdrant/*`
- Audio: `/audio/*`
- Translation: `/translation/*`

## Trust

Caddy stores its local CA in the `caddy_data` Docker volume. To trust it on the
host, copy/export the root CA from that volume or use a host Caddy install with
`caddy trust`. Do not commit private keys or generated certificates.

For production or public LAN use, switch `config/https.yaml` from `local_ca` to
a real ACME profile and bind only the gateway publicly.
