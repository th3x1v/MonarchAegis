# MonarchAegis

*A Crimson Elegy project.*

A self-hosted, database-driven **data-protection and replication platform** —
scheduled, catalog-backed replication between servers, with per-target policies,
integrity-checked transport, and a web control plane. Built to be approachable
and free of per-terabyte licensing.

> Status: active development. Replication core is in production use; versioning,
> point-in-time recovery, and broader workloads are on the roadmap.

## Features

- **DB-authoritative catalog** — the destination's record of "what's present" is
  the source of truth, so intentional destination changes (e.g. re-encodes) are
  never clobbered.
- **Scheduled, per-target replication** — interval presets + manual "Sync Now".
- **Two transports** — classic `rsync`, or a single integrity-checked **tar
  stream** over one SSH connection for many-small-file diffs.
- **Directory-jailed SSH transport** — each paired target is confined to its own
  receiving directory, write-only.
- **Web control plane** — targets, schedules, pairing, live per-target logs.

## Quick start

See [docs/setup_guide.md](docs/setup_guide.md) for full deployment instructions
(Docker / Unraid, Source + Client roles, pairing).

```bash
docker pull cthexiv/monarchaegis:latest
```

## License

MonarchAegis is licensed under the **GNU Affero General Public License v3.0 or
later (AGPL-3.0-or-later)** — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

The AGPL requires that anyone who runs a modified version — **including as a
network service** — make the corresponding source available to its users.

**Commercial licensing** is available separately for organizations that cannot
comply with the AGPL (proprietary embedding, closed-source hosted offerings).
Contact the copyright holder.

## Contributing

Contributions are welcome, but require agreeing to the Contributor License
Agreement first — see [CONTRIBUTING.md](CONTRIBUTING.md) and [CLA.md](CLA.md).
