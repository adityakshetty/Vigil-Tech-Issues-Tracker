from vigil_tracker.config import Config
from vigil_tracker.task_links import TaskLinkParser

DEFAULT_PATTERN = (
    r"https://studio\.mercor\.com/annotator/tasks/(task_[0-9a-f]{32})/?(\?\S*)?"
)
HEX = "0123456789abcdef0123456789abcdef"


def parser():
    return TaskLinkParser(DEFAULT_PATTERN)


def test_finds_canonical_link():
    p = parser()
    text = f"please look at https://studio.mercor.com/annotator/tasks/task_{HEX}/ thanks"
    link = p.first(text)
    assert link is not None
    assert link.task_id == f"task_{HEX}"
    assert link.normalized_url == (
        f"https://studio.mercor.com/annotator/tasks/task_{HEX}/"
    )


def test_tolerates_and_strips_query_string():
    p = parser()
    text = f"https://studio.mercor.com/annotator/tasks/task_{HEX}/?from_view=queue"
    link = p.first(text)
    assert link is not None
    assert link.task_id == f"task_{HEX}"
    # Query string is stripped in the normalized form.
    assert "?" not in link.normalized_url
    assert link.normalized_url.endswith(f"task_{HEX}/")


def test_no_link_returns_none():
    p = parser()
    assert p.first("just a normal message with no task") is None
    assert p.contains_task_link("nothing here") is False


def test_rejects_malformed_id():
    p = parser()
    # Too short / non-hex ids must not match.
    assert p.first("https://studio.mercor.com/annotator/tasks/task_xyz/") is None
    assert p.first("https://studio.mercor.com/annotator/tasks/task_123/") is None


def test_dedupes_repeated_task_id():
    p = parser()
    url = f"https://studio.mercor.com/annotator/tasks/task_{HEX}/"
    links = p.find_all(f"{url} and again {url}")
    assert len(links) == 1


def test_default_config_pattern_matches():
    # The pattern shipped in the default config must behave identically.
    cfg = Config.from_dict(
        {
            "slack": {"bot_token": "x"},
            "classification": {"api_key": "y"},
            "google": {"sheet_id": "z"},
        }
    )
    p = TaskLinkParser(cfg.task_link_pattern)
    assert p.contains_task_link(
        f"https://studio.mercor.com/annotator/tasks/task_{HEX}/"
    )
