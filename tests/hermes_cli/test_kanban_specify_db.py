"""Tests for kb.specify_triage_task — the DB-layer atomic promotion
from the triage column to todo. LLM-free by design."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _create_triage(conn, title="rough idea", body=None, assignee=None):
    return kb.create_task(
        conn,
        title=title,
        body=body,
        assignee=assignee,
        triage=True,
    )


def test_specify_promotes_triage_to_todo(kanban_home):
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="rough idea")
        assert kb.get_task(conn, tid).status == "triage"
    with kbc.connect() as conn:
        ok = kb.specify_triage_task(
            conn,
            tid,
            title="Refined: rough idea",
            body="**Goal**\nDo the thing.",
            author="specifier-bot",
        )
    assert ok is True
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
    # No parents → recompute_ready should have flipped it past todo to ready.
    assert task.status == "ready"
    assert task.title == "Refined: rough idea"
    assert "**Goal**" in (task.body or "")


def test_specify_rejects_blank_title(kanban_home):
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="rough")
    with kbc.connect() as conn, pytest.raises(ValueError):
        kb.specify_triage_task(conn, tid, title="   ", body="ok")


def test_specify_records_audit_comment_only_when_author_given(kanban_home):
    # With author → comment added.
    with kbc.connect() as conn:
        tid1 = _create_triage(conn, title="a")
        kb.specify_triage_task(
            conn, tid1, title="A-spec", body="b", author="ace"
        )
        comments1 = kb.list_comments(conn, tid1)
    assert len(comments1) == 1
    assert "Specified" in comments1[0].body
    assert comments1[0].author == "ace"

    # Without author → no comment (silent).
    with kbc.connect() as conn:
        tid2 = _create_triage(conn, title="b")
        kb.specify_triage_task(conn, tid2, title="B-spec", body="b")
        comments2 = kb.list_comments(conn, tid2)
    assert comments2 == []




@pytest.mark.parametrize("body", [
    "STATUS: HOLD. Answer in a live session.",
    "Paper-only question. Do not dispatch.",
    "keep it in triage until JY replies",
])
def test_held_triage_card_is_never_promoted_or_assigned(kanban_home, body):
    """Incident t_e2ebca73: a HOLD card was assigned+promoted by the specifier."""
    from hermes_cli import kanban_decompose, kanban_specify
    from hermes_cli.kanban_db_graph import decompose_triage_task

    with kbc.connect() as conn:
        tid = _create_triage(conn, title="held question", body=body)
        assert kb.specify_triage_task(conn, tid, body="rewritten", assignee="default") is False
        assert decompose_triage_task(
            conn, tid, root_assignee="default",
            children=[{"title": "c", "body": "", "assignee": "default", "parents": []}],
        ) is None
        task = kb.get_task(conn, tid)
    assert (task.status, task.assignee, task.body) == ("triage", None, body)
    # The dispatcher tick and CLI sweeps source ids from these lists.
    assert tid not in kanban_decompose.list_triage_ids()
    assert tid not in kanban_specify.list_triage_ids()
