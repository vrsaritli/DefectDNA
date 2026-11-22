from services.ai_knowledge_service.app.pipeline.scrub import scrub_text, to_plain_text


def test_html_is_converted_to_plain_text() -> None:
    text = to_plain_text("<div>Step&nbsp;1<br/>Step 2</div><p>Done &amp; dusted</p>")
    assert text == "Step 1\nStep 2\nDone & dusted"


def test_sensitive_values_are_replaced_and_counted() -> None:
    raw = (
        "Contact jane.doe@example.com or see https://intranet.example.com/wiki?id=7. "
        "Server db01.corp and 10.20.30.40:8080 failed. "
        "Authorization: Bearer abcdefghijklmnop1234 and password=Hunter2! "
        "key gsk_ABCDEFGHIJKLMNOP1234 token for user. "
        "Card 4111 1111 1111 1111, account 123456789, ref 3f2504e0-4f89-11d3-9a0c-0305e82c3301."
    )
    result = scrub_text(raw)

    for secret in (
        "jane.doe@example.com",
        "intranet.example.com",
        "db01.corp",
        "10.20.30.40",
        "abcdefghijklmnop1234",
        "Hunter2!",
        "gsk_ABCDEFGHIJKLMNOP1234",
        "4111 1111 1111 1111",
        "123456789",
        "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
    ):
        assert secret not in result.text
    assert {"email", "url", "host", "ip", "secret", "number", "id"} <= set(result.counts)


def test_ordinary_defect_text_is_preserved() -> None:
    raw = "Order total wrong after coupon SAVE10 removal in release 2026.3, defect #48213."
    assert scrub_text(raw).text == raw
    assert scrub_text(raw).counts == {}


def test_long_opaque_token_without_known_prefix_is_removed() -> None:
    token = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    assert token not in scrub_text(f"PAT {token} expired").text
