# Container Lifecycle Spec

`orchestrator.lifecycle` owns on-demand Docker container start/stop decisions for
managed ai-local services. It may start, stop, and observe registered Compose
services through the configured Docker boundary, but it must not execute feature
logic or storage/file semantics.

## Resource Pressure Reaping

When the Resource Governor reports memory or swap pressure, the lifecycle
manager may reduce ai-local's own footprint by stopping managed services that
are:

- not marked `always_on`;
- not currently starting;
- not actively serving a request;
- not required by another running managed service;
- idle for at least the configured pressure idle floor.

This is owner-safe cleanup. The lifecycle manager must not run global host
operations such as `swapoff`, `drop_caches`, Docker prune, killing unknown
processes, or changing host swap configuration. Those actions affect the whole
user machine and require a separate explicit operator contract.

Pressure reaping is generic: it is driven by Resource Governor snapshots,
central config, service registration and dependency metadata. It must not depend
on a benchmark, local folder, user-specific path, or scenario-specific prompt.

## Verification

Use focused lifecycle tests for service eligibility, active-request protection,
dependency protection and config resolution.
