"""Tests for SNS signature verification helpers in app.adapters.esp.ses."""

from __future__ import annotations

import base64

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from datetime import datetime, timedelta, timezone

from app.adapters.esp import ses as ses_mod


def test_host_allowlist_rejects_non_aws():
    assert not ses_mod._is_allowed_cert_host("https://evil.example.com/cert.pem")
    assert not ses_mod._is_allowed_cert_host("http://sns.us-east-1.amazonaws.com/cert.pem")
    assert not ses_mod._is_allowed_cert_host("https://amazonaws.com.evil.com/cert.pem")


def test_host_allowlist_accepts_aws():
    assert ses_mod._is_allowed_cert_host(
        "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-abc.pem"
    )


def _build_cert_and_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "test-sns")]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM)
    return pem, key


def _sign_message(key, canonical: str, algo=hashes.SHA256()) -> str:
    sig = key.sign(canonical.encode("utf-8"), padding.PKCS1v15(), algo)
    return base64.b64encode(sig).decode("ascii")


def test_verify_signature_good_and_bad(monkeypatch):
    pem, key = _build_cert_and_key()

    cert_url = "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-unit.pem"
    ses_mod._SNS_CERT_CACHE[cert_url] = pem

    msg = {
        "Type": "Notification",
        "MessageId": "abc-123",
        "TopicArn": "arn:aws:sns:us-east-1:000:fake",
        "Message": "hello",
        "Timestamp": "2026-04-22T00:00:00.000Z",
        "SignatureVersion": "2",
        "SigningCertURL": cert_url,
    }
    canonical = ses_mod._canonical_string_for_notification(msg)
    msg["Signature"] = _sign_message(key, canonical)

    assert ses_mod._verify_sns_signature(msg) is True

    bad = dict(msg)
    bad["Message"] = "tampered"
    assert ses_mod._verify_sns_signature(bad) is False


def test_verify_signature_rejects_bad_host(monkeypatch):
    pem, key = _build_cert_and_key()
    cert_url = "https://evil.example.com/cert.pem"
    ses_mod._SNS_CERT_CACHE[cert_url] = pem

    msg = {
        "Type": "Notification",
        "MessageId": "abc",
        "TopicArn": "arn",
        "Message": "x",
        "Timestamp": "t",
        "SignatureVersion": "2",
        "SigningCertURL": cert_url,
    }
    canonical = ses_mod._canonical_string_for_notification(msg)
    msg["Signature"] = _sign_message(key, canonical)

    # Must reject based on host regardless of valid signature.
    assert ses_mod._verify_sns_signature(msg) is False
