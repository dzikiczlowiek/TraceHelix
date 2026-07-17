from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import cast

import pytest

import tracehelix_training.redact as redact_module
from tracehelix_training.contracts import canonical_json_bytes
from tracehelix_training.redact import (
    ConfigError,
    InputValidationError,
    KeyCollisionError,
    LimitError,
    RedactionConfig,
    RedactionError,
    ScanFailedError,
    _post_scan_passes,
    load_config,
    redact,
)
from traceback_privacy import is_tracehelix_training_frame

CONFIG = Path(__file__).parents[1] / "configs" / "redaction-v1.yaml"


def cfg(**changes: object) -> dict[str, object]:
    value = cast(dict[str, object], json.loads(CONFIG.read_text()))
    value.update(changes)
    return value


def test_packaged_default_config_matches_the_single_source_policy() -> None:
    packaged = redact_module.load_default_config()
    source = load_config(CONFIG)
    assert packaged.config_hash == source.config_hash
    out, report = redact("owner@example.test", packaged)
    assert out == "<REDACTED:EMAIL:1>"
    assert report.version == "redaction-v1"


def test_nested_key_aware_copy_and_stability() -> None:
    token = "strange" + "-credential"
    source = {"items": [{"password": {"unexpected": token}}], "ok": True}
    original = copy.deepcopy(source)
    out, report = redact(source, cfg())
    assert source == original
    assert out == {"items": [{"password": "<REDACTED:KEYED:1>"}], "ok": True}
    assert report == redact(source, cfg())[1]
    assert report.total_replacements == 1
    assert report.secret_scan_passed is True
    assert token not in json.dumps(report.to_dict())


def test_all_text_families_and_false_positives() -> None:
    pieces = [
        "Bearer " + "abc.DEF-123",
        "Basic " + "dXNlcjpwYXNz",
        "eyJ" + "hbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature123",
        "sk-" + "A" * 24,
        "ghp_" + "B" * 36,
        "AKIA" + "C" * 16,
        "AWS_SECRET_ACCESS_KEY=" + "d" * 40,
        "-----BEGIN PRIVATE KEY-----\r\n" + "Z" * 32 + "\r\n-----END PRIVATE KEY-----",
        "postgres://user:" + "pass123@host/db",
        "Server=db;Password=" + "pass456;User=x",
        "API_TOKEN=" + "secret789",
        "person@example.test",
        "+44 20 7946 0958",
        "/home/alice/project",
    ]
    text = "\n".join(pieces)
    out, report = redact({"text": text}, cfg())
    encoded = json.dumps(out)
    for value in pieces:
        assert value not in encoded
    assert report.total_replacements >= len(pieces)
    safe = "order-id-2024 prose localhost http://localhost:8080 /var/lib/app 12345"
    assert redact(safe, cfg())[0] == safe


def test_extended_email_phone_and_windows_path_shapes() -> None:
    ipv6_email = "synthetic.user@[2001:db8::1]"
    dotted_phone = "+49.30.1234.5678"
    out, report = redact([ipv6_email, dotted_phone], cfg())
    assert out == ["<REDACTED:EMAIL:1>", "<REDACTED:PHONE:1>"]
    assert report.counts["EMAIL"] == report.counts["PHONE"] == 1

    windows_path = "C:\\Users\\synthetic-user\\repo"
    assert windows_path.count("\\") == 3
    path_out, path_report = redact(windows_path, cfg())
    assert path_out == "<REDACTED:USER_PATH:1>\\repo"
    assert path_report.counts == {"USER_PATH": 1}
    for safe_path in ["D:\\service\\repo", "C:\\ProgramData\\repo"]:
        assert redact(safe_path, cfg())[0] == safe_path


def test_numbering_repeat_policy_and_idempotence() -> None:
    mail = "same@example.test"
    out, first = redact([mail, mail, "other@example.test"], cfg())
    assert out == ["<REDACTED:EMAIL:1>", "<REDACTED:EMAIL:2>", "<REDACTED:EMAIL:3>"]
    again, second = redact(out, cfg())
    assert again == out
    assert second.total_replacements == 0
    assert first.input_hash != first.output_hash


def test_literal_counts_only_real_occurrences_and_numbers_each_repeat() -> None:
    literal = "synthetic-literal-canary"
    custom = cfg(literal_secrets=[literal])
    unchanged, absent = redact("ordinary text", custom)
    assert unchanged == "ordinary text"
    assert absent.total_replacements == 0
    assert "LITERAL" not in absent.counts

    out, repeated = redact(f"{literal}|{literal}", custom)
    assert out == "<REDACTED:LITERAL:1>|<REDACTED:LITERAL:2>"
    assert repeated.counts["LITERAL"] == 2
    assert repeated.total_replacements == 2


def test_genuine_active_placeholders_are_opaque_and_fully_idempotent() -> None:
    custom = cfg(patterns=[{"name": "ticket_secret", "regex": "ZZ[0-9]{6}"}])
    existing = "before <REDACTED:EMAIL:7> and <REDACTED:CUSTOM_TICKET_SECRET:3> after"
    out, report = redact(existing, custom)
    assert out == existing
    assert report.total_replacements == 0


def test_placeholder_conflicting_configuration_is_rejected_privately() -> None:
    canary = "REDACTED"
    conflicts = [
        cfg(literal_secrets=[canary]),
        cfg(patterns=[{"name": "placeholder_word", "regex": canary}]),
    ]
    for conflict in conflicts:
        with pytest.raises(ConfigError, match="invalid redaction configuration") as exc:
            redact("ordinary text", conflict)
        assert canary not in str(exc.value)


@pytest.mark.parametrize(
    ("config", "payload"),
    [
        (cfg(literal_secrets=["EMAIL:1234"]), "<REDACTED:EMAIL:1234>"),
        (
            cfg(patterns=[{"name": "count", "regex": r"CUSTOM_COUNT:1234"}]),
            {"outer": ["<REDACTED:CUSTOM_COUNT:1234>"]},
        ),
        (
            cfg(patterns=[{"name": "count", "regex": r"CUSTOM_COUNT:1234"}]),
            {"password": "<REDACTED:CUSTOM_COUNT:1234>"},
        ),
    ],
)
def test_unsampled_placeholder_conflicts_fail_closed_privately(
    config: dict[str, object], payload: object
) -> None:
    canary = "1234"
    with pytest.raises(RedactionError, match="redaction configuration") as caught:
        redact(payload, config)
    _assert_public_error_is_private(caught.value, canary, payload)


def test_fake_placeholder_kinds_fail_closed_or_are_key_redacted_without_disclosure() -> None:
    fake_aws = "<REDACTED:AKIAABCDEFGHIJKLMNOP:7>"
    fake_unknown = "<REDACTED:ATTACKER_KIND:2>"
    for value in [fake_aws, ["safe", {"nested": fake_unknown}]]:
        out, report = redact(value, cfg())
        encoded = json.dumps(out)
        assert fake_aws not in encoded
        assert fake_unknown not in encoded
        assert report.counts == {"KEYED": 1}

    out, _ = redact({"password": fake_unknown}, cfg())
    assert out == {"password": "<REDACTED:KEYED:1>"}


def test_keyed_placeholders_are_idempotent() -> None:
    out, first = redact({"password": "synthetic secret"}, cfg())
    again, second = redact(out, cfg())
    assert again == out == {"password": "<REDACTED:KEYED:1>"}
    assert first.total_replacements == 1
    assert second.total_replacements == 0


def test_hashes_use_the_common_plain_lowercase_sha256_format() -> None:
    source = {"safe": "value"}
    config = cfg()
    _, report = redact(source, config)
    assert report.input_hash == hashlib.sha256(canonical_json_bytes(source)).hexdigest()
    assert report.config_hash == hashlib.sha256(canonical_json_bytes(config)).hexdigest()
    assert len(report.output_hash) == 64
    assert all(character in "0123456789abcdef" for character in report.output_hash)


def test_string_key_redaction_and_collision() -> None:
    secret_key = "owner@example.test"
    out, _ = redact({secret_key: 1}, cfg())
    assert isinstance(out, dict)
    assert list(out) == ["<REDACTED:EMAIL:1>"]
    with pytest.raises(KeyCollisionError):
        redact({"<REDACTED:EMAIL:1>": 1, "owner@example.test": 2}, cfg())


@pytest.mark.parametrize("value", [{1, 2}, object(), {1: "x"}])
def test_unsupported(value: object) -> None:
    with pytest.raises(InputValidationError):
        redact(value, cfg())


def test_invalid_json_values_fail_as_safe_redaction_errors() -> None:
    invalid_values: list[object] = [
        10**5000,
        float("inf"),
        float("nan"),
        "synthetic-surrogate-canary-\ud800",
        object(),
        {1: "x"},
    ]
    for value in invalid_values:
        with pytest.raises(RedactionError):
            redact(value, cfg())


def test_depth_configuration_and_input_are_bounded_below_recursion_danger() -> None:
    too_deep: object = "leaf"
    for _ in range(1500):
        too_deep = [too_deep]
    with pytest.raises(ConfigError):
        redact(
            too_deep,
            cfg(
                limits={
                    "max_depth": 2000,
                    "max_nodes": 2000,
                    "max_string_chars": 20,
                    "max_input_bytes": 10000,
                }
            ),
        )

    boundary: object = "leaf"
    for _ in range(64):
        boundary = [boundary]
    redact(
        boundary,
        cfg(
            limits={
                "max_depth": 64,
                "max_nodes": 100,
                "max_string_chars": 20,
                "max_input_bytes": 1000,
            }
        ),
    )
    with pytest.raises(LimitError):
        redact(
            [boundary],
            cfg(
                limits={
                    "max_depth": 64,
                    "max_nodes": 100,
                    "max_string_chars": 20,
                    "max_input_bytes": 1000,
                }
            ),
        )
    with pytest.raises(ConfigError):
        load_config(
            cfg(
                limits={
                    "max_depth": 65,
                    "max_nodes": 100,
                    "max_string_chars": 20,
                    "max_input_bytes": 1000,
                }
            )
        )

    valid = load_config(cfg())
    forged = RedactionConfig(
        valid.version,
        {**valid.limits, "max_depth": 2000, "max_nodes": 2000},
        valid.literal_secrets,
        valid.patterns,
        valid.config_hash,
    )
    with pytest.raises(ConfigError):
        redact(too_deep, forged)


def test_direct_redaction_config_cannot_bypass_closed_policy_validation() -> None:
    valid = load_config(cfg())
    forged_configs = [
        RedactionConfig(
            "forged-version",
            valid.limits,
            valid.literal_secrets,
            valid.patterns,
            valid.config_hash,
        ),
        RedactionConfig(
            valid.version,
            valid.limits,
            valid.literal_secrets,
            valid.patterns,
            "f" * 64,
        ),
        RedactionConfig(
            valid.version,
            valid.limits,
            valid.literal_secrets,
            (("zero_width", re.compile(r"\B")),),
            valid.config_hash,
        ),
        RedactionConfig(
            valid.version,
            valid.limits,
            valid.literal_secrets,
            (("flagged", re.compile("secret", re.IGNORECASE)),),
            valid.config_hash,
        ),
    ]
    for forged in forged_configs:
        with pytest.raises(ConfigError):
            redact({"value": "abcdefghij"}, forged)


def test_cycles_and_limits() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(LimitError):
        redact(cyclic, cfg())
    with pytest.raises(LimitError):
        redact(
            [[["x"]]],
            cfg(
                limits={
                    "max_depth": 2,
                    "max_nodes": 100,
                    "max_string_chars": 20,
                    "max_input_bytes": 100,
                }
            ),
        )
    with pytest.raises(LimitError):
        redact(
            [1, 2],
            cfg(
                limits={
                    "max_depth": 5,
                    "max_nodes": 2,
                    "max_string_chars": 20,
                    "max_input_bytes": 100,
                }
            ),
        )
    with pytest.raises(LimitError):
        redact(
            "x" * 21,
            cfg(
                limits={
                    "max_depth": 5,
                    "max_nodes": 10,
                    "max_string_chars": 20,
                    "max_input_bytes": 100,
                }
            ),
        )
    with pytest.raises(LimitError):
        redact(
            "x" * 15,
            cfg(
                limits={
                    "max_depth": 5,
                    "max_nodes": 10,
                    "max_string_chars": 20,
                    "max_input_bytes": 10,
                }
            ),
        )


def test_custom_rules_and_invalid_configs() -> None:
    literal = "project" + "-canary"
    custom = cfg(
        literal_secrets=[literal], patterns=[{"name": "ticket_secret", "regex": "ZZ[0-9]{6}"}]
    )
    out, report = redact("x " + literal + " ZZ123456 ZZ12", custom)
    assert isinstance(out, str)
    assert literal not in out and "ZZ123456" not in out and "ZZ12" in out
    assert report.counts["LITERAL"] == 1

    repeated, repeated_report = redact("ZZ123456 ZZ123456", custom)
    assert repeated == ("<REDACTED:CUSTOM_TICKET_SECRET:1> <REDACTED:CUSTOM_TICKET_SECRET:2>")
    assert repeated_report.counts["CUSTOM_TICKET_SECRET"] == 2
    bad = [
        cfg(extra=True),
        cfg(literal_secrets=[""]),
        cfg(patterns=[{"name": "x", "regex": "x"}, {"name": "x", "regex": "y"}]),
        cfg(patterns=[{"name": "x", "regex": "("}]),
        cfg(patterns=[{"name": "x", "regex": "x*"}]),
        cfg(patterns=[{"name": "x", "regex": "(x+)+$"}]),
        cfg(patterns=[{"name": "x", "regex": "(x|xx)+$"}]),
        cfg(patterns=[{"name": "x", "regex": "x", "replacement": "unsafe"}]),
        cfg(limits={"max_depth": 0, "max_nodes": 1, "max_string_chars": 1, "max_input_bytes": 1}),
    ]
    for config in bad:
        with pytest.raises(ConfigError) as exc:
            load_config(config)
        assert "canary" not in str(exc.value)


def _assert_public_error_is_private(error: RedactionError, canary: str, sensitive: object) -> None:
    assert error.__context__ is None
    assert error.__cause__ is None
    assert canary not in str(error)
    assert canary not in repr(error)
    traceback = error.__traceback__
    while traceback is not None:
        if is_tracehelix_training_frame(
            traceback.tb_frame.f_code.co_filename, module_filename="redact.py"
        ):
            for local in traceback.tb_frame.f_locals.values():
                assert local is not sensitive
                assert canary not in repr(local)
        traceback = traceback.tb_next


def test_public_errors_do_not_retain_sensitive_config_or_input(tmp_path: Path) -> None:
    invalid_json_canary = "invalid-json-private-canary"
    invalid_path = tmp_path / "private-config.json"
    invalid_path.write_text('{"' + invalid_json_canary + '":', encoding="utf-8")

    direct_canary = "invalid-direct-private-canary"
    direct = cfg(extra=direct_canary)
    regex_canary = "invalid-regex-private-canary"
    invalid_regex = cfg(patterns=[{"name": "private", "regex": "(" + regex_canary}])
    unicode_canary = "invalid-unicode-private-canary"
    invalid_unicode = cfg(literal_secrets=[unicode_canary + "\ud800"])
    input_canary = "invalid-input-private-canary"
    invalid_input = {"value": input_canary + "\ud800"}

    cases: list[tuple[object, str, object, bool]] = [
        (invalid_path, invalid_json_canary, invalid_path, False),
        (direct, direct_canary, direct, False),
        (invalid_regex, regex_canary, invalid_regex, False),
        (invalid_unicode, unicode_canary, invalid_unicode, False),
        (invalid_input, input_canary, invalid_input, True),
    ]
    for payload, canary, sensitive, is_input in cases:
        with pytest.raises(RedactionError) as caught:
            if is_input:
                redact(payload, cfg())
            else:
                load_config(payload)  # type: ignore[arg-type]
        _assert_public_error_is_private(caught.value, canary, sensitive)


@pytest.mark.parametrize(
    "expression",
    [
        r"\Asecret",
        r"secret\Z",
        r"\bsecret",
        r"secret\B",
        r"(?=secret)secret",
        r"(?!safe)secret",
        r"(?<=x)secret",
        r"(?<!x)secret",
        r"(secret)",
        r"(?:secret)",
        r"(?i)secret",
        r"secret|token",
        r"(secret)\1",
        r"x{0}y",
        r"^$",
    ],
)
def test_custom_patterns_reject_nonconsuming_and_structural_constructs(
    expression: str,
) -> None:
    with pytest.raises(ConfigError):
        load_config(cfg(patterns=[{"name": "unsafe", "regex": expression}]))


def test_custom_pattern_fixed_width_boundaries_and_linear_probe() -> None:
    accepted = cfg(patterns=[{"name": "fixed", "regex": r"^[A-Z]{128}\-[0-9]{127}$"}])
    out, report = redact("A" * 128 + "-" + "7" * 127, accepted)
    assert out == "<REDACTED:CUSTOM_FIXED:1>"
    assert report.total_replacements == 1
    with pytest.raises(ConfigError):
        load_config(cfg(patterns=[{"name": "wide", "regex": "x{257}"}]))

    source = "x" * 100_000
    unchanged, _ = redact(source, cfg(patterns=[{"name": "maximum", "regex": "y{256}"}]))
    assert unchanged == source


def test_attacker_controlled_custom_replacement_is_rejected_without_leaking() -> None:
    replacement_canary = "sk-" + "R" * 24
    with pytest.raises(ConfigError) as exc:
        load_config(
            cfg(
                patterns=[{"name": "unsafe", "regex": "trigger", "replacement": replacement_canary}]
            )
        )
    assert replacement_canary not in str(exc.value)


def test_post_scan_covers_builtins_literals_and_configured_regexes() -> None:
    literal = "scan-literal-canary"
    custom = load_config(
        cfg(
            literal_secrets=[literal],
            patterns=[{"name": "scan_pattern", "regex": "ZX[0-9]{8}"}],
        )
    )
    builtin = "sk-" + "Q" * 24
    assert not _post_scan_passes({"value": builtin}, custom)
    assert not _post_scan_passes({"value": literal}, custom)
    assert not _post_scan_passes({"value": "ZX12345678"}, custom)
    assert _post_scan_passes(
        {"value": "safe <REDACTED:API_TOKEN:9> and <REDACTED:CUSTOM_SCAN_PATTERN:2>"},
        custom,
    )


def test_secret_assignments_preserve_prefixes_and_quotes_across_dotenv_lines() -> None:
    aws_value = "a" * 40
    source = "\n".join(
        [
            f'AwS_SeCrEt_AcCeSs_KeY = "{aws_value}"',
            "APP_PASSWORD='two words'",
            "password = secret",
            "PREFIX_TOKEN_SUFFIX=token-value",
            "SERVICE_API_KEY_VALUE = api-key-value",
            "prefixPwdSuffix=pwd-value",
            "Server=db;Password=connection-value;User=x",
        ]
    )
    out, report = redact(source, cfg())
    assert isinstance(out, str)
    assert 'AwS_SeCrEt_AcCeSs_KeY = "<REDACTED:AWS_SECRET:1>"' in out
    assert "APP_PASSWORD='<REDACTED:ENV_SECRET:1>'" in out
    assert "password = <REDACTED:CONNECTION_PASSWORD:1>" in out
    assert "PREFIX_TOKEN_SUFFIX=<REDACTED:ENV_SECRET:2>" in out
    assert "SERVICE_API_KEY_VALUE = <REDACTED:ENV_SECRET:3>" in out
    assert "prefixPwdSuffix=<REDACTED:ENV_SECRET:4>" in out
    assert "Password=<REDACTED:CONNECTION_PASSWORD:2>" in out
    assert all(
        value not in out
        for value in [
            aws_value,
            "two words",
            "token-value",
            "api-key-value",
            "pwd-value",
            "connection-value",
        ]
    )
    assert report.total_replacements == 7


def test_assignment_scanner_independently_rejects_partial_or_unhandled_values() -> None:
    custom = load_config(cfg())
    too_long = "APP_PASSWORD=" + "x" * 4097
    assert not _post_scan_passes(too_long, custom)
    with pytest.raises(ScanFailedError):
        redact(too_long, custom)
    prose = "The password = secret is an ordinary explanatory sentence."
    assert redact(prose, custom)[0] == prose


@pytest.mark.parametrize(
    "label",
    [
        "PRIVATE KEY",
        "RSA PRIVATE KEY",
        "EC PRIVATE KEY",
        "OPENSSH PRIVATE KEY",
        "ENCRYPTED PRIVATE KEY",
        "DSA PRIVATE KEY",
    ],
)
def test_all_standard_private_key_pem_labels_are_redacted_and_scanned(label: str) -> None:
    body = "\n".join(["U1lOVEhFVElDLU5PVC1SRUFM" * 3, "VEVTVC1LRVktTUFURVJJQUw=" * 3])
    pem = f"-----BEGIN {label}-----\n{body}\n-----END {label}-----"
    out, report = redact({"pem": pem}, cfg())
    assert pem not in json.dumps(out)
    assert out == {"pem": "<REDACTED:PRIVATE_KEY:1>"}
    assert report.counts == {"PRIVATE_KEY": 1}
    assert not _post_scan_passes(pem, load_config(cfg()))


def test_realistic_synthetic_canaries_cover_pem_dotenv_github_and_cloud_tokens() -> None:
    pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        + "U1lOVEhFVElDLVRFU1QtS0VZLU1BVEVSSUFMLU5PVC1SRUFM"
        + "\n-----END PRIVATE KEY-----"
    )
    dotenv_secrets = ["'synthetic-password-canary'", "synthetic-api-key-canary"]
    dotenv = "\n".join(
        [
            f"APP_PASSWORD={dotenv_secrets[0]}",
            f"SERVICE_API_KEY={dotenv_secrets[1]}",
        ]
    )
    github_classic = "ghp_" + "SYNTHETIC" * 4
    github_fine_grained = "github_pat_" + "SYNTHETIC_" * 3
    aws_access = "AKIA" + "SYNTHETICACCESS1"
    aws_secret = "AWS_SECRET_ACCESS_KEY=" + "s" * 40
    openai = "sk-" + "syntheticCloudTokenValue1"
    google = "AIza" + "SyntheticGoogleCloudTokenValue12345"
    canaries = [
        pem,
        dotenv,
        github_classic,
        github_fine_grained,
        aws_access,
        aws_secret,
        openai,
        google,
    ]

    out, report = redact({"payload": "\n".join(canaries)}, cfg())
    encoded = json.dumps(out)
    assert all(canary not in encoded for canary in [*canaries, *dotenv_secrets])
    assert report.counts["PRIVATE_KEY"] == 1
    assert report.counts["GITHUB_TOKEN"] == 2
    assert report.counts["AWS_ACCESS_KEY"] == 1
    assert report.counts["AWS_SECRET"] == 1
    assert report.counts["API_TOKEN"] == 1
    assert report.counts["GOOGLE_API_KEY"] == 1
    assert report.counts["ENV_SECRET"] == 2
