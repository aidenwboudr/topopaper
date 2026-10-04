import hashlib
import io
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from topopaper import paths, session, starter  # noqa: E402


@pytest.fixture
def data(tmp_path, monkeypatch):
    for v in ("TOPOPAPER_DIR", "TOPOPAPER_DATA", "XDG_STATE_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr(starter.util, "notify", lambda *a, **k: None)
    return tmp_path


def make_archive(path, members):
    """members: [(name, bytes)] -> tar.gz (stdlib-only for the test itself)."""
    with tarfile.open(path, "w:gz") as t:
        for name, blob in members:
            ti = tarfile.TarInfo(name)
            ti.size = len(blob)
            t.addfile(ti, io.BytesIO(blob))
    Path(str(path) + ".sha256").write_text(
        f"{hashlib.sha256(Path(path).read_bytes()).hexdigest()}  {Path(path).name}\n")
    return path


def test_fetch_from_file_url(data):
    arc = make_archive(data / "starter.tar.gz", [("areas/earth/terrain.bin", b"T" * 100),
                                                ("areas/earth/meta.json", b"{}")])
    assert starter.main(["--url", arc.as_uri()]) == 0
    assert (paths.areas_dir() / "earth" / "terrain.bin").read_bytes() == b"T" * 100
    assert paths.area_file().read_text().strip() == "earth"
    assert not [p for p in paths.data_dir().iterdir() if p.name.startswith(".starter-")]
    assert starter.main(["--url", arc.as_uri()]) == 0          # already there


def test_bad_checksum_is_refused(data):
    arc = make_archive(data / "s.tar.gz", [("areas/earth/terrain.bin", b"x")])
    Path(str(arc) + ".sha256").write_text("0" * 64 + "\n")
    with pytest.raises(ValueError, match="checksum"):
        starter.fetch(arc.as_uri())
    assert not (paths.areas_dir() / "earth").exists()


def test_path_traversal_is_refused(data):
    arc = make_archive(data / "evil.tar.gz", [("areas/earth/terrain.bin", b"x"),
                                             ("../../escaped", b"x")])
    with pytest.raises(ValueError, match="outside"):
        starter.extract(arc, data / "dest")
    assert not (data / "escaped").exists()


def test_symlinks_are_refused(data):
    p = data / "link.tar.gz"
    with tarfile.open(p, "w:gz") as t:
        ti = tarfile.TarInfo("areas/earth/terrain.bin")
        ti.type, ti.linkname = tarfile.SYMTYPE, "/etc/passwd"
        t.addfile(ti)
    with pytest.raises(ValueError, match="plain"):
        starter.extract(p, data / "dest")


def test_expected_sha():
    h = "a" * 64
    assert starter.expected_sha(f"{h}  file.tar.zst\n") == h
    assert starter.expected_sha(h.upper()) == h
    assert starter.expected_sha("nope") is None


def test_session_helpers(data, monkeypatch, tmp_path):
    eng = tmp_path / "eng"
    eng.write_text("#!/bin/sh\n")
    eng.chmod(0o755)
    monkeypatch.setenv("TOPOPAPER_ENGINE", str(eng))
    assert session.find_engine() == str(eng)
    (tmp_path / "rt").mkdir()
    assert session.is_running() is False
    lock = session.acquire_lock()
    assert lock is not None and session.is_running() is True
    assert session.acquire_lock() is None
    lock.close()
    assert session.is_running() is False
    assert session.stop() == 0                                  # nothing running: fine
