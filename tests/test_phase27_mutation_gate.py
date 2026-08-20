import pytest

from tools import run_mutation_gate


def test_each_mutation_sentinel_resolves_exactly_once(tmp_path):
    for mutation in run_mutation_gate.MUTATIONS:
        source = run_mutation_gate.REPO_ROOT / mutation.source
        destination = tmp_path / mutation.source
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        run_mutation_gate.apply_mutation(tmp_path, mutation)
        mutated = destination.read_text(encoding="utf-8")
        assert mutation.original not in mutated
        assert mutation.replacement in mutated


def test_mutation_application_fails_closed_when_source_drifts(tmp_path):
    mutation = run_mutation_gate.MUTATIONS[0]
    path = tmp_path / mutation.source
    path.parent.mkdir(parents=True)
    path.write_text("changed upstream", encoding="utf-8")
    with pytest.raises(RuntimeError, match="expected one source match"):
        run_mutation_gate.apply_mutation(tmp_path, mutation)


def test_mutation_gate_reports_surviving_and_killed_mutants(monkeypatch, capsys):
    mutation = run_mutation_gate.MUTATIONS[0]
    monkeypatch.setattr(run_mutation_gate, "_copy_repository", lambda _path: None)
    monkeypatch.setattr(run_mutation_gate, "apply_mutation", lambda *_args: None)
    monkeypatch.setattr(
        run_mutation_gate.subprocess,
        "run",
        lambda *_args, **_kwargs: type("Result", (), {"returncode": 1})(),
    )
    assert run_mutation_gate.run_mutation(mutation) is True
    assert "[KILLED]" in capsys.readouterr().out

    monkeypatch.setattr(
        run_mutation_gate.subprocess,
        "run",
        lambda *_args, **_kwargs: type("Result", (), {"returncode": 0})(),
    )
    assert run_mutation_gate.run_mutation(mutation) is False
    assert "[SURVIVED]" in capsys.readouterr().out
