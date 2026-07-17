"""Deterministic, offline redaction of bounded JSON-like values."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterator, Mapping, Match, TypeAlias, cast

from tracehelix_training._canonical import canonical_json_value_bytes

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class RedactionError(Exception):
    """Base class for safe redaction errors (messages contain no input data)."""


class ConfigError(RedactionError):
    """The closed redaction configuration is invalid."""


class LimitError(RedactionError):
    """Input is cyclic or exceeds a configured bound."""


class InputValidationError(RedactionError):
    """Input is not safely representable in the supported JSON value subset."""


class KeyCollisionError(RedactionError):
    """Distinct keys become equal after redaction."""


class ScanFailedError(RedactionError):
    """Post-redaction scanning found secret-shaped output."""


@dataclass(frozen=True)
class _Failure:
    error_type: type[RedactionError]
    message: str


@dataclass(frozen=True)
class RedactionConfig:
    version: str
    limits: Mapping[str, int]
    literal_secrets: tuple[str, ...]
    patterns: tuple[tuple[str, re.Pattern[str]], ...]
    config_hash: str


@dataclass(frozen=True)
class RedactionReport:
    version: str
    config_hash: str
    input_hash: str
    output_hash: str
    counts: Mapping[str, int]
    total_replacements: int
    secret_scan_passed: bool

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe report without secret values."""
        return {
            "version": self.version,
            "config_hash": self.config_hash,
            "input_hash": self.input_hash,
            "output_hash": self.output_hash,
            "counts": dict(self.counts),
            "total_replacements": self.total_replacements,
            "secret_scan_passed": self.secret_scan_passed,
        }


_LIMITS = {"max_depth", "max_nodes", "max_string_chars", "max_input_bytes"}
_LIMIT_CEILINGS = {
    "max_depth": 64,
    "max_nodes": 1_000_000,
    "max_string_chars": 10_000_000,
    "max_input_bytes": 100_000_000,
}
_MAX_INTEGER_BITS = 2048
_CONFIG_FIELDS = {"version", "limits", "literal_secrets", "patterns"}
_PATTERN_FIELDS = {"name", "regex"}
_PLACEHOLDER = re.compile(r"<REDACTED:(?P<kind>[A-Z0-9_]+):[1-9][0-9]*>")
_ASSIGNMENT_KINDS = frozenset({"AWS_SECRET", "CONNECTION_PASSWORD", "ENV_SECRET"})
_KEYED = re.compile(
    r"(?ix)(?:password|passwd|pwd|secret|token|api[ _-]?key|authorization|cookie|session|"
    r"private[ _-]?key|connection[ _-]?string)"
)
_SECRET_NAME = (
    r"(?:(?:PASSWORD|PASSWD|PWD|SECRET|TOKEN|API_KEY)[A-Za-z0-9_]*|"  # noqa: S105 -- detector vocabulary
    r"[A-Za-z_][A-Za-z0-9_]*(?:PASSWORD|PASSWD|PWD|SECRET|TOKEN|API_KEY)[A-Za-z0-9_]*)"
)
_ASSIGNMENT_PREFIX = rf"(?:^[ \t]*|[;,][ \t]*)(?P<name>{_SECRET_NAME})[ \t]*=[ \t]*"
_SECRET_ASSIGNMENT = re.compile(
    rf"(?P<prefix>{_ASSIGNMENT_PREFIX})(?:"
    r"'(?P<single>[^'\r\n]{1,4096})'|"
    r'"(?P<double>[^"\r\n]{1,4096})"|'
    r"(?P<bare>[^\s;,'\"]{1,4096}))",
    re.IGNORECASE | re.MULTILINE,
)
_SECRET_ASSIGNMENT_SCAN = re.compile(
    rf"{_ASSIGNMENT_PREFIX}(?P<value>[^\r\n;,]+)", re.IGNORECASE | re.MULTILINE
)

# Ordered broad-to-narrow only where overlap is intentional.
_BUILTINS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "PRIVATE_KEY",
        re.compile(
            r"-----BEGIN (?P<pem_label>(?:OPENSSH|RSA|EC|ENCRYPTED|DSA) PRIVATE KEY|PRIVATE KEY)-----"
            r"[\s\S]*?-----END (?P=pem_label)-----"
        ),
    ),
    ("AUTH", re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")),
    (
        "GITHUB_TOKEN",
        re.compile(r"\b(?:gh(?:p|o|u|s|r)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    ),
    ("API_TOKEN", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("GOOGLE_API_KEY", re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b")),
    ("AWS_ACCESS_KEY", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("URI_PASSWORD", re.compile(r"(?i)(?<=://)[^\s/:@]+:[^\s/@]+(?=@)")),
    (
        "EMAIL",
        re.compile(
            r"(?<![\w.+-])[\w.+-]{1,64}@(?:"
            r"[\w-]{1,63}(?:\.[\w-]{1,63}){1,10}|"
            r"\[(?:IPv6:)?[0-9A-Fa-f:.]{2,64}\])(?![\w-])"
        ),
    ),
    ("PHONE", re.compile(r"(?<!\w)\+[1-9][0-9 ().-]{8,30}[0-9](?!\w)")),
    ("USER_PATH", re.compile(r"(?:(?<![\w/])/(?:home|Users)/[^/\s]+|(?i:C:\\Users\\[^\\\s]+))")),
)


def _canonical(value: object) -> bytes:
    return canonical_json_value_bytes(value)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _validate_input(value: object, config: RedactionConfig) -> bytes:
    """Validate bounds and the JSON type/encoding subset before serialization."""
    active: set[int] = set()
    nodes = 0
    stack: list[tuple[object, int, bool]] = [(value, 0, False)]
    while stack:
        item, depth, leaving = stack.pop()
        if leaving:
            active.remove(id(item))
            continue
        nodes += 1
        if nodes > config.limits["max_nodes"] or depth > config.limits["max_depth"]:
            raise LimitError("input exceeds structural limits")
        if item is None or type(item) is bool:
            continue
        if type(item) is int:
            if item.bit_length() > _MAX_INTEGER_BITS:
                raise LimitError("input integer exceeds limit")
            continue
        if type(item) is float:
            if not math.isfinite(item):
                raise InputValidationError("input contains a non-finite number")
            continue
        if type(item) is str:
            text = item
            if len(text) > config.limits["max_string_chars"]:
                raise LimitError("input string exceeds limit")
            try:
                text.encode("utf-8")
            except UnicodeEncodeError:
                raise InputValidationError("input contains invalid Unicode") from None
            continue
        if type(item) not in (list, dict):
            raise InputValidationError("unsupported input type")
        identity = id(item)
        if identity in active:
            raise LimitError("cyclic input")
        active.add(identity)
        stack.append((item, depth, True))
        if type(item) is list:
            sequence = cast(list[object], item)
            stack.extend((child, depth + 1, False) for child in reversed(sequence))
            continue
        mapping = cast(dict[object, object], item)
        if any(type(key) is not str for key in mapping):
            raise InputValidationError("object keys must be strings")
        keys = cast(list[str], list(mapping))
        for key in keys:
            if len(key) > config.limits["max_string_chars"]:
                raise LimitError("input string exceeds limit")
            try:
                key.encode("utf-8")
            except UnicodeEncodeError:
                raise InputValidationError("input contains invalid Unicode") from None
        stack.extend((mapping[key], depth + 1, False) for key in reversed(sorted(keys)))
    try:
        encoded = _canonical(value)
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise InputValidationError("input cannot be canonically encoded") from None
    if len(encoded) > config.limits["max_input_bytes"]:
        raise LimitError("canonical input exceeds byte limit")
    return encoded


def _validate_bounded_pattern(expression: str) -> None:
    """Accept only documented, consuming, fixed-width regular expressions."""
    if len(expression) > 256:
        raise ConfigError("pattern exceeds complexity limits")
    index = 0
    width = 0
    repeatable = False
    escaped_literals = frozenset(r"\\.^$*+?{}[]|()-")
    consuming_escapes = frozenset("dDsSwWafnrtv")
    while index < len(expression):
        character = expression[index]
        if character == "\\":
            if index + 1 >= len(expression):
                raise ConfigError("invalid pattern")
            escaped = expression[index + 1]
            if escaped in "AZbB0123456789gk":
                raise ConfigError("pattern uses a non-consuming or referencing escape")
            if escaped == "x":
                digits = expression[index + 2 : index + 4]
                if len(digits) != 2 or any(
                    value not in "0123456789abcdefABCDEF" for value in digits
                ):
                    raise ConfigError("invalid pattern")
                index += 4
            elif escaped in consuming_escapes or escaped in escaped_literals:
                index += 2
            else:
                raise ConfigError("invalid pattern escape")
            width += 1
            repeatable = True
            continue
        if character == "[":
            index += 1
            if index < len(expression) and expression[index] == "^":
                index += 1
            class_start = index
            if index < len(expression) and expression[index] == "]":
                index += 1
            while index < len(expression) and expression[index] != "]":
                if expression[index] == "\\":
                    if index + 1 >= len(expression) or expression[index + 1] in "AZbB0123456789gk":
                        raise ConfigError("invalid character class escape")
                    index += 2
                else:
                    index += 1
            if index >= len(expression) or index == class_start:
                raise ConfigError("invalid pattern")
            index += 1
            width += 1
            repeatable = True
            continue
        if character == "{":
            end = expression.find("}", index + 1)
            count_text = expression[index + 1 : end] if end >= 0 else ""
            if not repeatable or not count_text.isascii() or not count_text.isdigit():
                raise ConfigError("pattern uses an unbounded construct")
            count = int(count_text)
            if count <= 0:
                raise ConfigError("pattern uses a non-consuming repeat")
            if count > 256:
                raise ConfigError("pattern exceeds complexity limits")
            width += count - 1
            repeatable = False
            index = end + 1
            if width > 256:
                raise ConfigError("pattern exceeds complexity limits")
            continue
        if character in ".()[]*+?|}":
            raise ConfigError("pattern uses an unsupported construct")
        if character in "^$":
            repeatable = False
        else:
            width += 1
            repeatable = True
        if width > 256:
            raise ConfigError("pattern exceeds complexity limits")
        index += 1
    if width == 0:
        raise ConfigError("pattern must consume text")


def _active_placeholder_kinds(config: RedactionConfig) -> frozenset[str]:
    kinds = {kind for kind, _ in _BUILTINS} | set(_ASSIGNMENT_KINDS) | {"KEYED"}
    if config.literal_secrets:
        kinds.add("LITERAL")
    kinds.update("CUSTOM_" + name.upper() for name, _ in config.patterns)
    return frozenset(kinds)


def _placeholder_conflicts(placeholder: str, config: RedactionConfig) -> bool:
    """Return whether a configured scanner claims this exact opaque placeholder."""
    return any(literal in placeholder for literal in config.literal_secrets) or any(
        pattern.search(placeholder) for _, pattern in config.patterns
    )


def _plain_segments(source: str, trusted_kinds: frozenset[str]) -> Iterator[str]:
    """Yield text outside placeholders generated by the active policy."""
    cursor = 0
    for match in _PLACEHOLDER.finditer(source):
        if match.group("kind") in trusted_kinds:
            yield source[cursor : match.start()]
            cursor = match.end()
    yield source[cursor:]


def _opaque_sub(
    pattern: re.Pattern[str],
    replacement: Callable[[Match[str]], str],
    source: str,
    trusted_kinds: frozenset[str],
) -> str:
    """Substitute outside active-policy placeholders, preserving only trusted kinds."""
    pieces: list[str] = []
    cursor = 0
    for match in _PLACEHOLDER.finditer(source):
        if match.group("kind") not in trusted_kinds:
            continue
        pieces.append(pattern.sub(replacement, source[cursor : match.start()]))
        pieces.append(match.group())
        cursor = match.end()
    pieces.append(pattern.sub(replacement, source[cursor:]))
    return "".join(pieces)


def _assignment_scan_passes(source: str, trusted_kinds: frozenset[str]) -> bool:
    """Independently reject non-placeholder values assigned to secret-like names."""
    for match in _SECRET_ASSIGNMENT_SCAN.finditer(source):
        value = match.group("value").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        placeholder = _PLACEHOLDER.fullmatch(value)
        if placeholder is None or placeholder.group("kind") not in trusted_kinds:
            return False
    return True


def _post_scan_passes(value: JsonValue, config: RedactionConfig) -> bool:
    """Return whether all non-placeholder output passes every configured scanner."""

    trusted_kinds = _active_placeholder_kinds(config)

    def text_passes(source: str) -> bool:
        if not _assignment_scan_passes(source, trusted_kinds):
            return False
        if any(
            match.group("kind") not in trusted_kinds
            or _placeholder_conflicts(match.group(), config)
            for match in _PLACEHOLDER.finditer(source)
        ):
            return False
        for candidate in _plain_segments(source, trusted_kinds):
            if any(literal in candidate for literal in config.literal_secrets):
                return False
            if any(pattern.search(candidate) for _, pattern in _BUILTINS):
                return False
            if any(pattern.search(candidate) for _, pattern in config.patterns):
                return False
        return True

    if isinstance(value, str):
        return text_passes(value)
    if isinstance(value, list):
        return all(_post_scan_passes(child, config) for child in value)
    if isinstance(value, dict):
        return all(
            text_passes(key) and _post_scan_passes(child, config) for key, child in value.items()
        )
    return True


def _load_config_result(
    source: Mapping[str, object] | str | Path,
) -> RedactionConfig | _Failure:
    try:
        raw = (
            json.loads(Path(source).read_text(encoding="utf-8"))
            if isinstance(source, (str, Path))
            else dict(source)
        )
        if not isinstance(raw, dict) or set(raw) != _CONFIG_FIELDS:
            raise ConfigError("invalid configuration fields")
        version, limits, literals, patterns = (
            raw["version"],
            raw["limits"],
            raw["literal_secrets"],
            raw["patterns"],
        )
        if not isinstance(version, str) or not re.fullmatch(r"redaction-v[1-9][0-9]*", version):
            raise ConfigError("invalid configuration version")
        if (
            not isinstance(limits, dict)
            or set(limits) != _LIMITS
            or any(
                type(value) is not int or value <= 0 or value > _LIMIT_CEILINGS[name]
                for name, value in limits.items()
            )
        ):
            raise ConfigError("invalid limits")
        if (
            not isinstance(literals, list)
            or len(literals) > 100
            or any(not isinstance(x, str) or not x or len(x) > 10000 for x in literals)
        ):
            raise ConfigError("invalid literal secrets")
        if not isinstance(patterns, list) or len(patterns) > 100:
            raise ConfigError("invalid patterns")
        compiled: list[tuple[str, re.Pattern[str]]] = []
        names: set[str] = set()
        for item in patterns:
            if (
                not isinstance(item, dict)
                or not set(item) <= _PATTERN_FIELDS
                or not {"name", "regex"} <= set(item)
            ):
                raise ConfigError("invalid pattern fields")
            name, expression = item["name"], item["regex"]
            if (
                not isinstance(name, str)
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name)
                or name in names
            ):
                raise ConfigError("invalid pattern name")
            if not isinstance(expression, str) or not expression:
                raise ConfigError("invalid pattern")
            _validate_bounded_pattern(expression)
            pattern = re.compile(expression)
            if pattern.search("") is not None:
                raise ConfigError("pattern may match empty text")
            names.add(name)
            compiled.append((name, pattern))
        canonical = cast(dict[str, object], raw)
        candidate = RedactionConfig(
            version,
            MappingProxyType(cast(dict[str, int], limits.copy())),
            tuple(cast(list[str], literals)),
            tuple(compiled),
            _digest(canonical),
        )
        # Active placeholders are opaque for idempotence, so configured scanners must
        # never claim their syntax. Reject such policies rather than shielding matches.
        placeholder_samples = [
            f"<REDACTED:{kind}:{count}>"
            for kind in _active_placeholder_kinds(candidate)
            for count in (1, 9, 10, 99, 100, 999, 1000, 9999999)
        ]
        if any(literal in sample for literal in literals for sample in placeholder_samples) or any(
            pattern.search(sample) for _, pattern in compiled for sample in placeholder_samples
        ):
            raise ConfigError("invalid redaction configuration")
        return candidate
    except ConfigError as error:
        return _Failure(type(error), str(error))
    except Exception:
        return _Failure(ConfigError, "invalid redaction configuration")


def load_config(source: Mapping[str, object] | str | Path) -> RedactionConfig:
    """Load and strictly validate a JSON-compatible YAML configuration."""
    result = _load_config_result(source)
    del source
    if isinstance(result, _Failure):
        raise result.error_type(result.message) from None
    return result


def _validate_config_instance(config: RedactionConfig) -> RedactionConfig:
    """Rebuild and verify a caller-supplied instance as a closed policy."""
    try:
        pattern_sources: list[dict[str, str]] = []
        supplied_flags: list[int] = []
        for item in config.patterns:
            if type(item) is not tuple or len(item) != 2:
                raise ConfigError("invalid redaction configuration")
            name, pattern = item
            if not isinstance(name, str) or not isinstance(pattern, re.Pattern):
                raise ConfigError("invalid redaction configuration")
            pattern_sources.append({"name": name, "regex": pattern.pattern})
            supplied_flags.append(pattern.flags)
        raw: dict[str, object] = {
            "version": config.version,
            "limits": dict(config.limits),
            "literal_secrets": list(config.literal_secrets),
            "patterns": pattern_sources,
        }
        rebuilt = _load_config_result(raw)
        if isinstance(rebuilt, _Failure):
            raise ConfigError("invalid redaction configuration")
        if config.config_hash != rebuilt.config_hash or supplied_flags != [
            pattern.flags for _, pattern in rebuilt.patterns
        ]:
            raise ConfigError("invalid redaction configuration")
        return rebuilt
    except ConfigError:
        raise
    except Exception:
        raise ConfigError("invalid redaction configuration") from None


def load_default_config() -> RedactionConfig:
    """Load the packaged default policy without relying on source-tree paths."""
    try:
        text = (
            resources.files("tracehelix_training")
            .joinpath("redaction-v1.json")
            .read_text(encoding="utf-8")
        )
        raw = cast(dict[str, object], json.loads(text))
    except (OSError, TypeError, ValueError, UnicodeError):
        raise ConfigError("packaged redaction configuration is invalid") from None
    del text
    return load_config(raw)


def _unsafe_redact(
    value: object, config: RedactionConfig | Mapping[str, object] | str | Path
) -> tuple[JsonValue, RedactionReport]:
    """Return a deep redacted copy and immutable, secret-free report."""
    cfg = (
        _validate_config_instance(config)
        if isinstance(config, RedactionConfig)
        else load_config(config)
    )
    trusted_kinds = _active_placeholder_kinds(cfg)
    counts: dict[str, int] = {}
    active: set[int] = set()
    nodes = 0

    def placeholder(kind: str) -> str:
        counts[kind] = counts.get(kind, 0) + 1
        return f"<REDACTED:{kind}:{counts[kind]}>"

    def replacement_for(kind: str) -> Callable[[Match[str]], str]:
        def replace(_match: Match[str]) -> str:
            return placeholder(kind)

        return replace

    def assignment_replacement(match: Match[str]) -> str:
        name = match.group("name").upper()
        kind = (
            "AWS_SECRET"
            if name == "AWS_SECRET_ACCESS_KEY"
            else "CONNECTION_PASSWORD"
            if name in {"PASSWORD", "PASSWD", "PWD"}
            else "ENV_SECRET"
        )
        quote = "'" if match.group("single") is not None else '"' if match.group("double") else ""
        return match.group("prefix") + quote + placeholder(kind) + quote

    def text(source: str) -> str:
        # A recognized placeholder is opaque only when this exact spelling cannot
        # shield a configured match. Counts are unbounded, so enforce this at runtime.
        if any(
            match.group("kind") in trusted_kinds and _placeholder_conflicts(match.group(), cfg)
            for match in _PLACEHOLDER.finditer(source)
        ):
            raise ConfigError("invalid redaction configuration")
        # Placeholder-shaped attacker input is itself sensitive opaque data. Preserve
        # active kinds, but replace every unknown kind as a whole before any scanner.
        result = _PLACEHOLDER.sub(
            lambda match: (
                match.group() if match.group("kind") in trusted_kinds else placeholder("KEYED")
            ),
            source,
        )
        result = _opaque_sub(_SECRET_ASSIGNMENT, assignment_replacement, result, trusted_kinds)
        for literal in cfg.literal_secrets:
            result = _opaque_sub(
                re.compile(re.escape(literal)), replacement_for("LITERAL"), result, trusted_kinds
            )
        for kind, pattern in _BUILTINS:
            result = _opaque_sub(pattern, replacement_for(kind), result, trusted_kinds)
        for name, pattern in cfg.patterns:
            result = _opaque_sub(
                pattern, replacement_for("CUSTOM_" + name.upper()), result, trusted_kinds
            )
        return result

    def walk(item: object, depth: int) -> JsonValue:
        nonlocal nodes
        nodes += 1
        if nodes > cfg.limits["max_nodes"] or depth > cfg.limits["max_depth"]:
            raise LimitError("input exceeds structural limits")
        if item is None or type(item) in (bool, int):
            return cast(JsonScalar, item)
        if type(item) is float:
            return item
        if type(item) is str:
            if len(item) > cfg.limits["max_string_chars"]:
                raise LimitError("input string exceeds limit")
            return text(item)
        if type(item) not in (list, dict):
            raise InputValidationError("unsupported input type")
        identity = id(item)
        if identity in active:
            raise LimitError("cyclic input")
        active.add(identity)
        try:
            if isinstance(item, list):
                return [walk(child, depth + 1) for child in item]
            result: dict[str, JsonValue] = {}
            mapping = cast(dict[object, object], item)
            if any(type(key) is not str for key in mapping):
                raise InputValidationError("object keys must be strings")
            for key in sorted(cast(dict[str, object], mapping)):
                redacted_key = text(key)
                if redacted_key in result:
                    raise KeyCollisionError("redacted object key collision")
                child = mapping[key]
                keyed = _KEYED.search(key) is not None
                child_placeholder = (
                    _PLACEHOLDER.fullmatch(child) if keyed and isinstance(child, str) else None
                )
                if (
                    child_placeholder is not None
                    and child_placeholder.group("kind") in trusted_kinds
                    and _placeholder_conflicts(child_placeholder.group(), cfg)
                ):
                    raise ConfigError("invalid redaction configuration")
                result[redacted_key] = (
                    cast(JsonValue, child)
                    if child_placeholder is not None
                    and child_placeholder.group("kind") in trusted_kinds
                    else placeholder("KEYED")
                    if keyed
                    else walk(child, depth + 1)
                )
            return result
        finally:
            active.remove(identity)

    input_bytes = _validate_input(value, cfg)
    output = walk(value, 0)

    if not _post_scan_passes(output, cfg):
        raise ScanFailedError("post-redaction secret scan failed")
    output_bytes = _canonical(output)
    immutable_counts = MappingProxyType(dict(sorted(counts.items())))
    report = RedactionReport(
        cfg.version,
        cfg.config_hash,
        _digest_bytes(input_bytes),
        _digest_bytes(output_bytes),
        immutable_counts,
        sum(counts.values()),
        True,
    )
    return output, report


def _redact_result(
    value: object, config: RedactionConfig | Mapping[str, object] | str | Path
) -> tuple[JsonValue, RedactionReport] | _Failure:
    try:
        return _unsafe_redact(value, config)
    except RedactionError as error:
        return _Failure(type(error), str(error))
    except Exception:
        return _Failure(InputValidationError, "redaction input could not be safely processed")


def redact(
    value: object, config: RedactionConfig | Mapping[str, object] | str | Path
) -> tuple[JsonValue, RedactionReport]:
    """Return a deep redacted copy and immutable, secret-free report."""
    result = _redact_result(value, config)
    del value, config
    if isinstance(result, _Failure):
        raise result.error_type(result.message) from None
    return result
