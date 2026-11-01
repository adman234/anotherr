import os

from anotherr import unraid


def test_locate_and_state(tmp_path, monkeypatch):
    mnt = tmp_path / "mnt"
    (mnt / "user" / "tv" / "Show").mkdir(parents=True)
    (mnt / "disk2" / "tv" / "Show").mkdir(parents=True)
    (mnt / "disk1").mkdir()
    (mnt / "disk2" / "tv" / "Show" / "ep.mkv").write_bytes(b"x")
    (mnt / "user" / "tv" / "Show" / "ep.mkv").write_bytes(b"x")
    monkeypatch.setattr(unraid, "MNT", str(mnt))
    unraid._dir_cache.clear()
    disk, path = unraid.locate(str(mnt / "user" / "tv" / "Show" / "ep.mkv"))
    assert disk == "disk2" and path == str(mnt / "disk2" / "tv" / "Show" / "ep.mkv")
    assert [os.path.basename(r) for r in unraid.disk_roots()] == ["disk1", "disk2"]

    em = tmp_path / "emhttp"
    em.mkdir()
    (em / "disks.ini").write_text('["disk1"]\nname="disk1"\nspundown="1"\nrotational="1"\n["disk2"]\nname="disk2"\nspundown="0"\n')
    (em / "var.ini").write_text('mdResyncPos="12345"\n')
    monkeypatch.setattr(unraid, "EMHTTP", str(em))
    assert unraid.spun_up_disks() == {"disk2"}
    assert unraid.parity_running() is True
