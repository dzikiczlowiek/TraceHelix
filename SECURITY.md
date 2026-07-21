# Security policy

## Supported versions

TraceHelix is a local, single-user source release. Security fixes target the
latest tagged source release on the `main` branch. The planned `v0.1.0` source
release is not yet tagged or published; until it is, the reviewed commit on the
`main` branch is the reference tree. There are no long-term-support branches,
and unsupported commits receive no backports. See
[`CHANGELOG.md`](CHANGELOG.md) for the planned `v0.1.0` scope.

## Trust model

TraceHelix is designed for a local trusted single-user workstation. The API and
browser UI bind to loopback only by default, and state-changing browser requests
are same-origin checked in production. These controls reduce accidental network
exposure; they are not authentication, authorization, or isolation against a
hostile process running on the same host or under the same user account. Do not
expose the API through a reverse proxy, tunnel, or network interface, and do not
deploy it as a multi-user or SaaS service. See
[`docs/architecture.md`](docs/architecture.md) and
[`docs/release-readiness-v0.1.0.md`](docs/release-readiness-v0.1.0.md) for the
bounded scope and the explicit open limitations.

## Reporting a vulnerability

Report suspected vulnerabilities privately through GitHub Security Advisories
("Report a vulnerability" on the Security tab of this repository). Do not open a
public GitHub issue, pull request, or comment for a suspected vulnerability.
Please include:

- the exact commit and working-tree fingerprint you reviewed (run
  `python scripts/source_fingerprint.py` and attach the value);
- a minimal reproduction under the intended local trust model;
- the observed impact and any mitigations you applied.

A maintainer will acknowledge privately received advisories and coordinate a fix
and disclosure timeline. No dedicated security email address is published;
private reporting is handled through GitHub Security Advisories only.

## What not to include

Do not include traces, secrets, credentials, API keys, tokens, private keys,
paths, prompts, or any private data in a public GitHub issue, pull request,
comment, or advisory. Trace records may contain authorization headers, cookies,
private keys, prompts, and other raw evidence; keep raw traces local and redact
or paraphrase before sharing any excerpt. The offline `redaction-v1` policy
governs only training-candidate export, not default CLI/API reports, so treat any
report artifact as potentially containing raw private values.
