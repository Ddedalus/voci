"""`voci._affected.world`: building a `resolve.World` over the whole first-party tree
(`plans/affected-tests-plan.md`, M3's "still to come" driver bullet)."""

from __future__ import annotations

from pathlib import Path

from voci._affected.world import build_world, first_party_files


def test_first_party_files_finds_every_py_file_under_rootdir(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mod.py").write_text("def test_x(): pass\n")

    found = first_party_files(tmp_path)

    assert set(found) == {
        (tmp_path / "pkg" / "__init__.py").resolve(),
        (tmp_path / "pkg" / "mod.py").resolve(),
        (tmp_path / "tests" / "test_mod.py").resolve(),
    }


def test_first_party_files_excludes_a_venv_by_pyvenv_cfg_regardless_of_its_name(
    tmp_path: Path,
) -> None:
    """`.venv-3.13`, not `.venv` -- `discover_files`' own `DEFAULT_IGNORE_DIRS` doesn't name it,
    so only `is_first_party`'s `pyvenv.cfg` walk keeps its third-party packages out."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n")
    venv = tmp_path / ".venv-3.13"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("home = /usr\n")
    site_packages = venv / "lib" / "site-packages" / "somelib"
    site_packages.mkdir(parents=True)
    (site_packages / "__init__.py").write_text("y = 1\n")

    found = first_party_files(tmp_path)

    assert found == [(tmp_path / "pkg" / "mod.py").resolve()]


def test_first_party_files_excludes_default_ignore_dirs(tmp_path: Path) -> None:
    (tmp_path / "pkg.py").write_text("x = 1\n")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "pkg.cpython-314.py").write_text("# not real source\n")

    found = first_party_files(tmp_path)

    assert found == [(tmp_path / "pkg.py").resolve()]


def test_build_world_resolves_a_name_across_two_first_party_files(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("from b import x\n")
    (tmp_path / "b.py").write_text("x = 1\n")

    world, files = build_world(tmp_path)

    a_path = (tmp_path / "a.py").resolve()
    b_path = (tmp_path / "b.py").resolve()
    assert set(files) == {a_path, b_path}
    assert files[a_path][0] == "a"
    assert files[b_path][0] == "b"
    from voci._affected.resolve import NameKey

    seeds = world.resolve_code(a_path, "<module>")
    closure = world.closure(seeds)
    assert NameKey(b_path, "x") in closure


def test_build_world_skips_a_file_that_cannot_be_decoded(tmp_path: Path) -> None:
    (tmp_path / "good.py").write_text("x = 1\n")
    # Declares latin-1 but holds a byte sequence that isn't valid under it after decoding as
    # UTF-8 fallback would garble -- write raw bytes that are invalid under the declared codec.
    bad = tmp_path / "bad.py"
    bad.write_bytes(b"# coding: ascii\nx = '\xff'\n")

    world, files = build_world(tmp_path)

    assert (tmp_path / "good.py").resolve() in files
    assert (tmp_path / "bad.py").resolve() not in files
    assert world is not None
