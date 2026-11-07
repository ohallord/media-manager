# Security policy

## Reporting a vulnerability

Do not open a public issue, pull request, or discussion for a suspected vulnerability, and do not paste proof-of-concept details in public.

Private vulnerability reporting on this GitHub repository has not been confirmed as enabled.

If it is enabled, the private report form is:

<https://github.com/ohallord/media-manager/security/advisories/new>

If that page does not let you file a private report, stop. Do not turn the report into a public issue. Wait until a private channel is confirmed.

When a private channel is available, include:

- the part you tested (portal, host watcher, H.265 worker, install scripts, or the Helm chart) and the version or commit
- what you did, or what an attacker would need
- the effect you observed

This policy does not set a response time, a fix deadline, or a list of supported versions.

## Supported versions

No supported-version rule is stated here. A tag listed in the changelog is a recorded version bump, not a promise that the version still receives fixes.

## Scope

In scope: the Media Manager portal, the host watcher, the H.265 worker, the published install and setup scripts, and the Helm chart in `k8s/helm/`.

Out of scope:

- Radarr, Sonarr, SABnzbd, Syncthing, Plex, Emby, Jellyfin, TMDb, Fanart.tv, and other separate products the portal talks to
- exposing the portal without its login, or publishing the admin password that setup prints once
- behavior already described under Known limits in the README

This repository does not include a separate security-audit report.
