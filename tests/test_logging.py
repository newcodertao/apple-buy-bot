import logging

from src.core.logging import redact, safe_url, setup_logging


def test_url_and_contact_redaction():
    assert (
        safe_url("https://user:password@www.apple.com/cn/?token=abc#secret")
        == "https://www.apple.com/cn/"
    )
    text = redact("phone=13812345678 id=110101199001011234 email=abc@example.com cookie=secret")
    for secret in ("13812345678", "110101199001011234", "abc@example.com", "cookie=secret"):
        assert secret not in text


def test_log_milliseconds_and_exception_suppression(tmp_path):
    setup_logging(tmp_path)
    try:
        raise ValueError("never-log-secret")
    except ValueError:
        logging.getLogger("apple").exception("action=inspect result=failed cookie=private")
    output = (tmp_path / "apple.log").read_text(encoding="utf-8")
    assert "ValueError" in output
    assert "never-log-secret" not in output and "private" not in output
    assert output[19] == "." and output[20:23].isdigit()
