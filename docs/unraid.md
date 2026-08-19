# Unraid setup

MetaFusion includes an [Unraid Docker template](../unraid/metafusion.xml) with
all supported environment variables. Basic view shows the required and common
settings; optional tuning and safety controls are under **Advanced View**.
Review [Kometa and Plex operation modes](modes.md) before choosing media or
Kometa path mappings. All guides are listed in the
[documentation index](index.md).

## Install the template

Until MetaFusion is available through Community Applications, place the
template in Unraid's user-template directory from the Unraid terminal:

```bash
mkdir -p /boot/config/plugins/dockerMan/templates-user
curl -fsSL \
  https://raw.githubusercontent.com/ray-cys/metafusion/main/unraid/metafusion.xml \
  -o /boot/config/plugins/dockerMan/templates-user/my-MetaFusion.xml
```

Refresh the **Docker** page, select **Add Container**, and choose MetaFusion
from the template list. Review every path before applying it.

The template uses:

```text
ghcr.io/ray-cys/metafusion:latest
```

For testing current `develop` changes, temporarily change **Repository** to:

```text
ghcr.io/ray-cys/metafusion:develop
```

When upgrading an existing template to Phase 19 or later, open **Advanced
View** and set **Concurrency Safety Ceiling** to `0`. Unraid preserves the old
saved value of `8`; that value is safe and still adaptive, but limits automatic
growth to eight workers.

Phase 20 defaults **Plex Libraries** to `auto`. Existing exact saved names
remain valid and continue to limit processing to those libraries.

## Required settings

Provide these connection and workflow values:

| Unraid field | Environment variable | Requirement |
| --- | --- | --- |
| Output Mode | `RUN_MODE` | `kometa` or `plex` |
| Plex URL | `PLEX_URL` | Complete server URL including port |
| Plex Token | `PLEX_TOKEN` | Owner/admin token; masked by the template |
| Plex Libraries | `PLEX_LIBRARIES` | `auto` discovers all movie/show libraries; exact comma-separated names limit scope |
| TMDb API Key | `TMDB_API_KEY` | TMDb v3 API key; masked by the template |
| Appdata Config Path | `/config` | Persistent writable appdata directory |

Fanart.tv artwork fallback uses MetaFusion's bundled project integration key.
There is no Fanart.tv key field to configure in the Unraid template.

The default appdata mapping is:

```text
/mnt/user/appdata/metafusion -> /config
```

`/config` contains the state databases, logs, reports, run lock, and managed
configuration template. Keep it persistent and include it in appdata backups.

## Unraid ownership and permissions

Keep the standard Unraid runtime identity:

```text
PUID=99
PGID=100
```

This is `nobody:users`. The appdata path and enabled output paths must be
writable by `99:100`. New files created by MetaFusion use mode `0664`, which
means read/write for owner and group and read-only for others.

Do not set a Docker user, replace the image entrypoint, or remove the template's
startup parameters. An explicit Docker user bypasses `PUID`/`PGID` handling and
can recreate the root-owned-file problem. The supplied parameters also provide
the read-only root filesystem, writable `/tmp`, init process, stop timeout, and
`no-new-privileges` protection.

Startup prepares only MetaFusion-managed state below `/config`. It does not
recursively `chown` or `chmod` Kometa assets, metadata YAML, or Plex media.
Existing output files keep their ownership and permissions unless an artwork
or metadata operation legitimately replaces that individual file.

If the app reports that `/config` is not writable, correct the host appdata
directory once from the Unraid terminal:

```bash
chown -R 99:100 /mnt/user/appdata/metafusion
chmod -R u+rwX,g+rwX /mnt/user/appdata/metafusion
```

Confirm the path before running these commands; do not apply them to an entire
media or appdata root.

## Kometa mode paths

For `RUN_MODE=kometa`:

- Keep the **Kometa Output Path** mapping.
- Point its host side to the directory Kometa can read.
- Keep its container target `/kometa`.
- Keep `KOMETA_PATH=/kometa`.
- Leave `PLEX_METADATA_UPDATES=False`.

Example:

```text
/mnt/user/appdata/kometa -> /kometa
```

MetaFusion writes YAML under `/kometa/metadata` and assets under
`/kometa/assets`. It does not write into Plex media directories in this mode.

## Plex mode paths

For `RUN_MODE=plex`, MetaFusion never creates Kometa YAML. If local artwork is
enabled, add writable path mappings for every media root.

Example Docker path:

```text
Host:      /mnt/user/media
Container: /media
Access:    Read/Write
```

If Plex reports `/data/Movies/Film/file.mkv` while MetaFusion sees the same
file under `/media/Movies/Film/file.mkv`, set:

```text
PLEX_PATH_MAPPINGS=/data=>/media
```

For multiple roots:

```text
PLEX_PATH_MAPPINGS=/data=>/media;/archive=>/archive
```

Add a matching Unraid path mapping for every right-hand container path. The
variable translates Plex paths but cannot expose host files to the container.
Resolved directories must already exist and be writable; MetaFusion refuses to
create missing media roots that may indicate a bad mapping.

Run `python metafusion.py --preflight` in the container console after adding
the mounts. It samples a bounded number of Plex paths and may print a suggested
mapping when one visible container root matches unambiguously. Review that
suggestion; MetaFusion never creates or edits the Unraid path mapping itself.

Direct Plex metadata updates do not need media mappings when
`RUN_POSTER=False`, `RUN_SEASON=False`, and `RUN_BACKGROUND=False`. They remain
disabled unless `PLEX_METADATA_UPDATES=True`.

## Environment-only or `config.yml`

The Unraid template can supply the entire configuration through environment
variables. In that case:

- `/config/config.yml` does not need to exist.
- MetaFusion does not create it.
- The maintained `/config/config_template.yml` contains placeholders only,
  never your environment values or secrets.

To use YAML instead, stop the container and create a separate file:

```bash
cp /mnt/user/appdata/metafusion/config_template.yml \
   /mnt/user/appdata/metafusion/config.yml
```

Edit `config.yml`, then start the container. Do not edit
`config_template.yml`; image updates may refresh it. Non-empty template
environment values have higher priority than YAML, so remove optional
variables from the Unraid container when `config.yml` should control them.

The [configuration reference](configuration.md) explains priority, every
variable, and per-library overrides.

## First-run safety

Recommended initial values are:

```text
DRY_RUN=True
RUN_CLEANUP=False
ASSET_UPDATE_POLICY=managed
```

If direct Plex metadata enrichment is enabled, also begin with:

```text
PLEX_METADATA_POLICY=fill_missing
PLEX_METADATA_MAX_WRITES_PER_RUN=25
```

Start the container and inspect **Logs**. After reviewing the dry-run results,
set `DRY_RUN=False`. Cleanup should remain disabled until a complete dry-run
reconciliation reports the expected removals. See [Policies](policies.md).

## Scheduling and manual runs

For a normal Unraid service:

```text
METAFUSION_RUN=False
RUN_SCHEDULE=True
RUN_ON_START=False
```

`RUN_TIMES` is interpreted in `TZ`. Appdata backup stops do not reset the saved
schedule or full-scan timestamps. When enabled, `SCHEDULE_CATCH_UP` can run one
recently missed slot after the container restarts.

To force a one-time run without permanently changing the container, open the
MetaFusion console and run:

```bash
python metafusion.py --metafusion_run
```

Useful console checks are:

```bash
python metafusion.py --doctor
python metafusion.py --status
python metafusion.py --support-report
```

See [Operations](operations.md) for targeted repairs and diagnostics.

## Update or roll back

For a stable installation, set **Repository** to an exact release such as:

```text
ghcr.io/ray-cys/metafusion:1.2.3
```

Apply the template and restart. To roll back, change only the tag to the prior
exact version or an immutable `sha-<full-commit>` tag. Keep `PUID=99`,
`PGID=100`, appdata, Kometa, and media mappings unchanged.

Use `ghcr.io/ray-cys/metafusion:develop` for soak testing only. Return to an
exact release or `latest` after the tested changes reach `main`.
