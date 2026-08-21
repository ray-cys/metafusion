# MetaFusion support and version policy

## Supported versions

| Release lane | Support level |
| --- | --- |
| Latest stable `vX.Y.Z` release | Fully supported; production bug and compatibility fixes are prioritized here. |
| Current release candidate | Supported for testing and release-blocking reports. Do not treat it as a stable production image. |
| `develop` image | Best-effort testing support only. Its behavior and state schema may change before release. |
| Older stable releases | Security and critical data-safety fixes only until the next stable release has been available for 90 days. |
| Older RC, SHA, or branch images | Unsupported except when requested to reproduce or diagnose a current issue. |

Use an exact stable version for production. `main`, `latest`, and `develop` are
moving tags; `sha-<full-commit>` is intended for exact diagnosis.

## Requesting support

Search the existing [GitHub issues](https://github.com/ray-cys/metafusion/issues)
before opening a report. Then use the
[GitHub issue chooser](https://github.com/ray-cys/metafusion/issues/new/choose)
to select Docker/Unraid, artwork/identity, Kometa output, Plex metadata,
runtime/cleanup, feature request, or general bug reporting.

Include:

- the exact image tag and commit shown by `python metafusion.py --version`;
- the selected operation mode and affected media type;
- relevant redacted log lines;
- a redacted support report or the problem-specific report requested by the
  selected form (explain when it is unavailable); and
- clear reproduction steps and the expected result.

Use the [diagnostics guide](docs/diagnostics.md) to choose any additional
library, artwork, metadata, mapping, identity, cleanup, state, or
release-qualification report that matches the problem. Do not attach every
report by default, and feature requests do not require diagnostic output.

Never publish Plex tokens, TMDb keys, `config.yml`, Docker inspection output,
private host paths, or unredacted metadata values. Support requests for modified
images or source trees should include the exact changes needed to reproduce the
problem.
