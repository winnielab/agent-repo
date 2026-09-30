from repopilot.memory.working import WorkingMemory


def test_working_memory_persists_explicit_fields_without_duplicates(tmp_path) -> None:
    path = tmp_path / "memory.json"
    memory = WorkingMemory.load(path)
    memory.remember("relevant_files", "service.py")
    memory.remember("hypotheses", "ordering is unstable")
    memory.remember("hypotheses", "ordering is unstable")
    memory.remember("test_failures", "expected 2 got 3")
    memory.remember("invalidated_assumptions", "late formatting preserves parsed state")
    loaded = WorkingMemory.load(path)
    assert loaded.relevant_files == {"service.py"}
    assert loaded.hypotheses == ["ordering is unstable"]
    assert "expected 2" in loaded.prompt_context()
    assert loaded.invalidated_assumptions == ["late formatting preserves parsed state"]
