from concurrent.futures import ThreadPoolExecutor

from applypilot.chat_journal import ChatJournal


def test_reply_reservation_survives_restart_and_uncertainty(tmp_path):
    path = tmp_path / "private" / "chat.sqlite3"
    journal = ChatJournal(path)
    assert journal.reserve("account", "123", "456", "a" * 64, "Reply")
    assert ChatJournal(path).status("account", "123", "a" * 64) == "unknown"
    assert not ChatJournal(path).reserve("account", "123", "456", "a" * 64, "Different")
    journal.finish("account", "123", "a" * 64, "sent")
    assert journal.status("account", "123", "a" * 64) == "sent"
    assert path.stat().st_mode & 0o777 == 0o600
    assert journal.reserve("account", "123", "456", "b" * 64, "Reply")


def test_concurrent_reservation_only_one_wins(tmp_path):
    journal = ChatJournal(tmp_path / "chat.sqlite3")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: journal.reserve("a", "1", "2", "k" * 64, "r"), range(2))
        )
    assert sorted(results) == [False, True]


def test_finish_is_idempotent_and_does_not_overwrite_terminal_state(tmp_path):
    journal = ChatJournal(tmp_path / "chat.sqlite3")
    assert journal.reserve("a", "1", "2", "k" * 64, "r")
    assert journal.finish("a", "1", "k" * 64, "unknown")
    assert not journal.finish("a", "1", "k" * 64, "sent")
    assert journal.status("a", "1", "k" * 64) == "unknown"
    assert journal.entry("a", "1", "k" * 64)["state"] == "unknown"


def test_unsupported_outcome_is_rejected(tmp_path):
    journal = ChatJournal(tmp_path / "chat.sqlite3")
    try:
        journal.finish("a", "1", "k" * 64, "completed")
    except ValueError:
        pass
    else:
        raise AssertionError("unsupported journal state was accepted")
