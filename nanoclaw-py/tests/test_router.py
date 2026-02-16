"""Tests for message formatting and routing — port of src/formatting.test.ts."""

from nanoclaw.models import NewMessage
from nanoclaw.router import escape_xml, format_messages, strip_internal_tags


def test_escape_xml_basic():
    assert escape_xml("hello") == "hello"
    assert escape_xml("<b>bold</b>") == "&lt;b&gt;bold&lt;/b&gt;"
    assert escape_xml('a & "b"') == 'a &amp; &quot;b&quot;'


def test_format_messages_single():
    msgs = [
        NewMessage(
            id="1", chat_jid="test@local", sender="u1",
            sender_name="Alice", content="Hello!", timestamp="2026-01-01T00:00:00Z",
        )
    ]
    result = format_messages(msgs)
    assert "<messages>" in result
    assert 'sender="Alice"' in result
    assert "Hello!" in result
    assert "</messages>" in result


def test_format_messages_multiple():
    msgs = [
        NewMessage(id="1", chat_jid="t", sender="u1", sender_name="A", content="Hi", timestamp="t1"),
        NewMessage(id="2", chat_jid="t", sender="u2", sender_name="B", content="Hey", timestamp="t2"),
    ]
    result = format_messages(msgs)
    assert result.count("<message ") == 2


def test_format_messages_xml_escape():
    msgs = [
        NewMessage(
            id="1", chat_jid="t", sender="u1",
            sender_name='O"Brien', content="a < b & c > d", timestamp="t1",
        )
    ]
    result = format_messages(msgs)
    assert 'O&quot;Brien' in result
    assert "a &lt; b &amp; c &gt; d" in result


def test_strip_internal_tags():
    assert strip_internal_tags("Hello <internal>secret</internal> world") == "Hello  world"
    assert strip_internal_tags("No tags here") == "No tags here"
    assert strip_internal_tags("<internal>all hidden</internal>") == ""
    # Multi-line internal
    assert strip_internal_tags("before <internal>\nmulti\nline\n</internal> after") == "before  after"
