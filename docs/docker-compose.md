# Docker Compose setup

This guide installs MetaFusion with the repository's hardened
`docker-compose.yml`. For Unraid, use the separate [Unraid guide](unraid.md).

## 1. Prepare the project

Docker Compose v2 is required.

```bash
git clone https://github.com/ray-cys/metafusion.git
cd metafusion
cp .env.example .env
mkdir -p config kometa
```

The image runs with a read-only root filesystem. Persistent state belongs in
`/config`; temporary files and the health heartbeat use `/tmp`.

## 2. Choose the output mode

### Kometa output

Use this when a separate Kometa installation will consume MetaFusion's YAML
and assets:

```text
RUN_MODE=kometa
KOMETA_HOST_PATH=./kometa
KOMETA_PATH=/kometa
PLEX_METADATA_UPDATES=False
```

Change `KOMETA_HOST_PATH` to the host directory used by Kometa. The default
Compose file maps it to `/kometa` in the container.

### Plex local artwork

Plex mode never writes Kometa YAML. When artwork generation is enabled, mount
every media root read/write. Keep site-specific mappings in an override file
so repository updates do not replace them:

```yaml
# docker-compose.override.yml
services:
  metafusion:
    volumes:
      - /srv/media:/media:rw
```

Then configure:

```text
RUN_MODE=plex
PLEX_METADATA_UPDATES=False
PLEX_PATH_MAPPINGS=/plex/server/path=>/media
```

The left side of each mapping is the path Plex reports. The right side is the
matching container mount. Multiple mappings are separated with semicolons:

```text
PLEX_PATH_MAPPINGS=/mnt/user/media=>/media;/mnt/disks/archive=>/archive
```

`PLEX_PATH_MAPPINGS` translates names only; it never creates a Docker mount.
The translated directory must already exist and be writable. If Plex and
MetaFusion see the same absolute paths, no translation is needed, but the
matching path still must be mounted.

Direct Plex metadata enrichment does not require media mounts when every
artwork option is disabled. It remains off unless
`PLEX_METADATA_UPDATES=True`.

## 3. Configure connections

Edit `.env` and provide at least:

```text
RUN_MODE=kometa
PLEX_URL=http://192.168.1.10:32400
PLEX_TOKEN=your-token
PLEX_LIBRARIES=Movies,TV Shows
TMDB_API_KEY=your-key
TZ=Asia/Singapore
```

Use exact Plex library names. A connection can use an IP address, Docker DNS
name, or another URL reachable from the MetaFusion container.

The complete variable list is in [Configuration](configuration.md). Keep
`PLEX_TOKEN` and `TMDB_API_KEY` out of screenshots and support attachments.
The optional `*_FILE` settings can read credentials from mounted secret files.

## 4. Set filesystem permissions

The default Compose runtime identity is `10001:10001`. The mapped config and
output directories must be writable by that identity, or set `PUID` and `PGID`
to an existing non-root host identity that owns the paths.

```text
PUID=10001
PGID=10001
```

Do not add Compose `user:` and do not replace the image entrypoint. Either
setting bypasses the image's `PUID`/`PGID` startup handling. Startup adjusts
only MetaFusion-managed state under `/config`; it does not recursively change
ownership or permissions across Kometa or media trees.

New MetaFusion files use mode `0664`. Existing metadata and artwork retain
their current ownership and permissions until MetaFusion atomically replaces
an eligible file; the replacement is created by the configured runtime
identity.

## 5. Start and validate

```bash
docker compose pull metafusion
docker compose up -d metafusion
docker compose logs -f metafusion
```

Validate the effective configuration without contacting Plex or TMDb:

```bash
docker compose run --rm metafusion python metafusion.py --doctor
```

Show scheduler state and recent jobs:

```bash
docker compose exec metafusion python metafusion.py --status
```

The long-running service normally uses `METAFUSION_RUN=False` and
`RUN_SCHEDULE=True`. To run one temporary job without changing the service:

```bash
docker compose run --rm -e METAFUSION_RUN=True metafusion
```

See [Operations](operations.md) for targeted repairs, dry runs, support
reports, cleanup, and health checks.

## Optional `config.yml`

Environment-only installations do not need `config.yml`, and MetaFusion never
creates one from environment values. The container maintains a current,
value-free reference at `config/config_template.yml`.

To use YAML configuration:

```bash
cp config/config_template.yml config/config.yml
# Edit config/config.yml, then validate and restart.
docker compose run --rm metafusion python metafusion.py --doctor
docker compose restart metafusion
```

Do not edit `config_template.yml`; it is refreshed when the image contains a
newer template. Existing `config.yml` files are never replaced. Non-empty
environment variables override matching YAML values, so remove or blank an
environment binding when YAML should supply that setting.

## Update or roll back

Production deployments should pin an exact release in `.env`:

```text
METAFUSION_IMAGE=ghcr.io/ray-cys/metafusion:1.2.3
```

Use `develop` only for testing changes before promotion to `main`:

```text
METAFUSION_IMAGE=ghcr.io/ray-cys/metafusion:develop
```

Apply an update:

```bash
docker compose pull metafusion
docker compose up -d metafusion
docker compose exec metafusion python metafusion.py --status
```

To roll back, change only `METAFUSION_IMAGE` to the previous exact release or
an immutable `sha-<full-commit>` tag and run the same commands. Do not delete
or recreate `/config`, `/kometa`, or media mappings during rollback.
