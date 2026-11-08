# Threat model

This document covers what Media Manager protects, who it trusts, and what it does not defend against. Report anything that breaks these assumptions privately, using the [security policy](https://github.com/ohallord/media-manager/security/policy).

## What it protects

- **Accounts and sessions.** User passwords, passkeys, and session cookies.
- **Integration credentials.** Radarr, Sonarr, SABnzbd, TMDb, Fanart.tv, Syncthing, Plex, Emby, Jellyfin, and SMTP URLs and API keys. They are stored encrypted in the `IntegrationConfig` table.
- **The media library.** The mover and the cleanup wizard can move and delete files. The transcoder can replace originals.
- **Request history and logs.** These show who asked for what.

## Who is trusted

| Actor | Trust |
| --- | --- |
| Admin | Full control: integrations, users, logs, scans, deletions. |
| Logged-in user | Browses the library and discovery pages and requests titles. Any logged-in user can add a title to Radarr or Sonarr without approval. This is a deliberate choice. |
| Anonymous visitor | Reaches only the login, 2FA, and password-recovery endpoints. Every integration route needs a session. |
| Integrated services | Trusted to return well-formed data, but their text is escaped before it reaches the page. |
| Host operator | Has the `.env` file, the database, and the media mounts. Out of scope. |

## Controls

- **Fail-closed bootstrap.** There is no default password. The first admin exists only when `INITIAL_ADMIN_PASSWORD` is set. That admin must change the password at first login.
- **Sessions.** JWT cookies are `HttpOnly`, `SameSite=Lax`, and `Secure` over HTTPS. Each password change revokes older tokens. Disabled users' tokens are ignored.
- **One-time codes.** 2FA and recovery codes come from `secrets`, are compared in constant time, expire after 10 minutes, lock out after 5 wrong guesses, and are rate-limited per user. Password recovery returns the same response for unknown users.
- **Authorization.** Admin actions depend on `get_admin_user`. `/add` only accepts root folders that Radarr or Sonarr already list.
- **Output escaping.** Untrusted text goes through `escapeHtml` before it reaches `innerHTML`.
- **Supply chain.** CI runs tests, a coverage gate, a dependency license check, and an SBOM build on every pull request. Dependabot, CodeQL, and secret scanning with push protection run on GitHub.

## Not defended against

- A portal exposed to the internet without TLS. Put it behind HTTPS. See [docs/INSTALL.md](INSTALL.md) for the Helm ingress TLS settings.
- Someone with shell access to the host, the database file, or `.env`.
- A compromised Radarr, Sonarr, or media server account. Media Manager acts on what those services report.
- Pending one-time codes are held in memory, so a restart cancels them. That is by design, not a vulnerability.

## Incident response

1. **Receive.** Reports arrive through GitHub private vulnerability reporting. Acknowledge within the time in the security policy.
2. **Assess.** Reproduce the report, confirm the affected versions, and rate its severity in a private security advisory.
3. **Fix.** Develop the fix privately, out of public branches and issues. Add a test that fails without the fix.
4. **Release.** Ship a patched release, publish the advisory with a CVE if one applies, and credit the reporter unless they ask not to be named.
5. **Contain.** If a credential leaked, rotate it first, then remove it from the source.
6. **Review.** Update this document if the incident exposed a wrong assumption.
