from importlib.util import module_from_spec, spec_from_file_location


def load_replay():
    spec = spec_from_file_location("replay_mcp_session", "scripts/replay_mcp_session.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_replay_default_overwrites_and_append_is_explicit(tmp_path):
    replay = load_replay()
    path = tmp_path / "session.jsonl"
    with replay.open_transcript(path, False) as handle:
        handle.write("old\n")
    with replay.open_transcript(path, False) as handle:
        handle.write("new\n")
    assert path.read_text(encoding="utf-8") == "new\n"
    with replay.open_transcript(path, True) as handle:
        handle.write("appended\n")
    assert path.read_text(encoding="utf-8") == "new\nappended\n"

