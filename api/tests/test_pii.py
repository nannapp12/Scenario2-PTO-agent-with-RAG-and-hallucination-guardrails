import base64

import pytest
from cryptography.exceptions import InvalidTag

from common.pii import PiiCipher, aad, mask_email, mask_name

KEY1 = base64.b64encode(b"1" * 32).decode()
KEY2 = base64.b64encode(b"2" * 32).decode()


def test_round_trip_and_ciphertext_hides_plaintext():
    c = PiiCipher(KEY1)
    blob = c.encrypt("Jane Doe", aad("E1", "full_name"))
    assert b"Jane" not in blob
    assert c.decrypt(blob, aad("E1", "full_name")) == "Jane Doe"
    assert c.encrypt("Jane Doe", aad("E1", "full_name")) != blob  # random nonce


def test_ciphertext_is_bound_to_employee_and_field():
    c = PiiCipher(KEY1)
    blob = c.encrypt("Jane Doe", aad("E1", "full_name"))
    with pytest.raises(InvalidTag):
        c.decrypt(blob, aad("E2", "full_name"))
    with pytest.raises(InvalidTag):
        c.decrypt(blob, aad("E1", "email"))


def test_key_rotation_reads_old_ciphertext():
    old = PiiCipher(KEY1).encrypt("x@y.org", aad("E1", "email"))
    rotated = PiiCipher(KEY2, previous_key_b64=KEY1)
    assert rotated.decrypt(old, aad("E1", "email")) == "x@y.org"
    with pytest.raises(ValueError):
        PiiCipher(KEY2).decrypt(old, aad("E1", "email"))


def test_rejects_short_key():
    with pytest.raises(ValueError):
        PiiCipher(base64.b64encode(b"short").decode())


def test_none_passes_through():
    c = PiiCipher(KEY1)
    assert c.encrypt(None, "a") is None and c.decrypt(None, "a") is None


def test_masking():
    assert mask_name("Jane Q Doe") == "J*** Q*** D***"
    assert mask_email("jane.doe@contoso.com") == "j***@contoso.com"
    assert mask_name(None) is None and mask_email("not-an-email") is None
