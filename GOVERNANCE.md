# Governance

Media Manager is a single-maintainer project. The maintainer makes the final call on what gets merged and released. This is often called the BDFL model.

## Maintainers

| Name | GitHub | Role |
| --- | --- | --- |
| Dan O'Halloran | [profile](https://github.com/ohallord) | Maintainer and admin |

There is no second admin yet. A backup admin is wanted, so the project isn't stranded if one account is lost. If you've contributed for a while and want to help, say so in [Discussions](https://github.com/ohallord/media-manager/discussions).

## How decisions are made

- Small fixes and docs changes: a pull request that passes CI and gets a maintainer review.
- Changes in behavior, new features, or breaking changes: open an issue or a discussion first. The maintainer decides after the discussion.
- Ties and disagreements: the maintainer decides and writes the reason on the issue or pull request.
- The [roadmap](ROADMAP.md) lists what is planned and what was decided against.

## How changes reach a release

Every change to `main` goes through a pull request. CI must pass, and a pull request needs one approving review. Releases are cut by the maintainer and noted in [CHANGELOG.md](CHANGELOG.md).

## Becoming a maintainer

A contributor with a record of reviewed, merged pull requests can be invited to help with reviews and triage. Write access follows, then admin access, at the maintainer's discretion. Access is reviewed when someone becomes inactive, and is removed if it is no longer needed.

## Code of conduct and security

Everyone follows the [Code of Conduct](CODE_OF_CONDUCT.md). Report vulnerabilities privately as described in the [security policy](https://github.com/ohallord/media-manager/security/policy). The [threat model](docs/THREAT_MODEL.md) explains what the project defends against.

## Changing this document

Changes to governance go through a pull request, like any other change.
