# Offline redaction policy

TraceHelix applies the versioned `redaction-v1` policy before a trace can become a
training candidate. Redaction is deterministic and offline: it makes no network,
cloud, or model calls. The policy removes recognized credentials, PII, user paths,
and values beneath secret-indicating keys, then runs a built-in secret-shape scan.
Only output whose report says `secret_scan_passed: true` may be consumed by cloud
calls or exported to downstream training systems. Scan failure is fail-closed and
produces neither output nor a valid report.

The source-tree policy is `training/configs/redaction-v1.yaml`; despite the extension, policy
syntax is strict JSON-compatible YAML (the JSON subset), not arbitrary YAML. An identical
`redaction-v1.json` package resource is included in the wheel, checked by a parity regression,
and loaded with `tracehelix_training.redact.load_default_config()` so installed consumers do
not depend on a repository path. The policy is closed and bounded. `max_depth` cannot exceed
64, and integers wider than 2,048 bits are rejected before JSON conversion. Project-specific
literal values and named regular expressions can be added locally; they must be treated as
sensitive configuration and must not be logged.

Custom replacement text is forbidden: every match becomes a generated per-occurrence
placeholder. Configured regular expressions are limited to consuming fixed-width literal
concatenation, consuming character classes/escapes, `^`/`$` anchors, and exact positive
`{n}` repeats. Expression and expanded width are each at most 256 characters. `{0}`, empty
matches, assertion escapes (`\\A`, `\\Z`, `\\b`, `\\B`), backreferences, inline flags,
groups, branches, lookarounds, dots, and variable quantifiers are rejected.

Configuration, input, and output hashes use the training contract's canonical UTF-8 JSON and
plain, 64-character lowercase SHA-256 hexadecimal format. Placeholders are numbered per type
in sorted-key depth-first traversal order. Repeated occurrences receive distinct numbers.
Only placeholders whose kinds the active policy can generate are opaque and stable on a
subsequent pass, including when embedded in longer strings. The closed vocabulary consists of
built-in and assignment kinds, `KEYED`, `LITERAL` when literals are configured, and active
`CUSTOM_<SANITIZED_NAME>` kinds. Placeholder-shaped input with any other kind is replaced as
a whole. Literal or custom-regex configuration that would match active placeholder syntax is
rejected, preventing placeholder opacity from shielding configured secrets. Secret-like
dotenv/connection assignments are recognized
case-insensitively at line starts or after `;`/`,` delimiters; their prefixes and quotes are
preserved while only bounded values are replaced. A separate post-scan rejects leftover
assignment values. The fail-closed post-scan also checks built-in shapes, configured literals,
and configured regular expressions outside placeholders.

Supported built-ins are deliberately finite: PKCS#8, RSA, EC, OpenSSH, encrypted PKCS#8, and
DSA private-key PEM blocks; Basic/Bearer values,
JWTs, selected GitHub/OpenAI-style/Google/AWS token shapes, URI passwords, the documented
secret-like assignments, conventional and bracketed-IPv6 email shapes, international
`+`-prefixed phone shapes using spaces, parentheses, hyphens, or dots, and `/home`, `/Users`,
or `C:\\Users` user components. Key-aware matching covers names containing password/passwd/
pwd, secret, token, API key, authorization, cookie, session, private key, or connection
string. Beneath such a key the complete value is replaced, including structured values.
Only string keys are supported; non-string keys fail validation. Redacted-key collisions fail
closed.

This scope is not universal credential or PII detection. Redaction reduces disclosure risk;
it is **not proof of anonymity**. Context can identify people even after direct identifiers
are removed, and conservative detectors can miss novel formats. Raw traces must remain local
and private, with access and retention kept
to the minimum required. Review policy changes and scan-passed candidate data before use.
Never place raw matching values in logs, exceptions, reports, snapshots, or fixtures.
