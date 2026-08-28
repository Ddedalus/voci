"""Tests reading the root conftest's annotated fixtures, plus a builtin and a name collision.

Contributes the case where the consuming module binds the annotation's own name itself
(`Session`, a helper here), which is the one an import has to be aliased for, and the builtin
case, whose type comes from velox rather than from anything the suite wrote.
"""


class Session:
    """A helper that happens to be named like the type next door."""

    dsn = "not the fixture's"


def test_session_states_its_type(session):
    assert session.dsn == "sqlite:///showcase"


def test_widget_states_what_it_yields(widget):
    assert widget.name == "spindle"


def test_catalogue_states_a_mapping(catalogue):
    assert "spindle" in catalogue


def test_a_builtin_is_typed_by_velox(tmp_path, capsys):
    print("written")
    (tmp_path / "note.txt").write_text("written")

    assert capsys.readouterr().out == "written\n"


async def test_channel_states_what_it_yields(channel):
    channel.append("message")

    assert channel == ["message"]


def test_nothing_to_recover(untyped, opaque):
    assert untyped["nothing"]
    assert opaque is not None
