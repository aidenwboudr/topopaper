"""`topopaper-ctl starter`: make sure the globe (the `earth` pack) exists.

Every route ends in `earth`, so it is the one pack topopaper can't do
without. Downloading the prebuilt one (~30 MB, checked against its .sha256)
takes seconds; building it from elevation tiles takes a long while, so that
is only the fallback (or `--build`).

The archive holds `areas/earth/...` and is unpacked into the data dir. Any
member that would land outside it, or that isn't a plain file/directory, is
refused.
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

from . import net, paths, util

URL = ("https://github.com/aidenwboudr/topopaper/releases/download/starter-v1/"
       "topopaper-starter-v1.tar.zst")


def log(msg):
    print(msg, flush=True)


def download(url, dest, label="", timeout=60):
    """Stream `url` (http(s) or file://) to `dest`, printing progress."""
    req = urllib.request.Request(url, headers=net.UA)
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got, last = 0, 0.0
        while True:
            b = r.read(1 << 16)
            if not b:
                break
            f.write(b)
            got += len(b)
            if time.monotonic() - last > 0.5:
                last = time.monotonic()
                pct = f" {100 * got // total:3d}%" if total else ""
                log(f"  {label}{pct} {got / 1e6:6.1f} MB")
    log(f"  {label} done, {got / 1e6:.1f} MB")
    return got


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def expected_sha(text):
    """First 64-hex token of a .sha256 file (`sha256sum` format or bare)."""
    for tok in text.split():
        if len(tok) == 64 and all(c in "0123456789abcdefABCDEF" for c in tok):
            return tok.lower()
    return None


def zstd_reader(path):
    """(stream, proc) of the decompressed tar, or None to let tar(1) do it.
    Python 3.14's compression.zstd, then the zstandard module, then zstd(1)."""
    try:
        from compression import zstd
        return zstd.open(path, "rb"), None
    except ImportError:
        pass
    try:
        import zstandard
        return zstandard.ZstdDecompressor().stream_reader(open(path, "rb")), None
    except ImportError:
        pass
    if shutil.which("zstd"):
        p = subprocess.Popen(["zstd", "-dc", str(path)], stdout=subprocess.PIPE)
        return p.stdout, p
    return None


def safe_members(tar, dest):
    """Members, or ValueError on anything that escapes `dest` or isn't a
    regular file/directory (links, devices)."""
    root = os.path.realpath(dest)
    out = []
    for m in tar:
        target = os.path.realpath(os.path.join(root, m.name))
        if os.path.commonpath([root, target]) != root:
            raise ValueError(f"refusing {m.name!r}: outside the target")
        if not (m.isfile() or m.isdir()):
            raise ValueError(f"refusing {m.name!r}: not a plain file")
        out.append(m)
    return out


def extract(archive, dest):
    """Unpack a .tar.zst (or plain/gz/xz tar) into dest, safely."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    name = str(archive)
    proc = None
    if name.endswith((".zst", ".zstd")):
        got = zstd_reader(archive)
        if got is None:
            return _extract_with_tar(archive, dest)
        stream, proc = got
        tar = tarfile.open(fileobj=stream, mode="r|")
    else:
        tar = tarfile.open(archive, mode="r:*")
    try:
        with tar:
            # a stream can't be listed twice: check each member as it comes
            for m in tar:
                safe_members([m], dest)
                tar.extract(m, dest, set_attrs=False, **_filter())
    finally:
        if proc:
            proc.stdout.close()
            if proc.wait() != 0:
                raise ValueError("zstd failed to decompress the archive")
    return True


def _filter():
    return {"filter": "data"} if hasattr(tarfile, "data_filter") else {}


def _extract_with_tar(archive, dest):
    """No zstandard module or zstd binary: GNU tar's own --zstd (which itself
    needs zstd...), after listing the names for the same safety check."""
    names = subprocess.run(["tar", "--zstd", "-tvf", str(archive)], capture_output=True,
                           text=True, check=True).stdout.splitlines()
    root = os.path.realpath(dest)
    for ln in names:
        kind, name = ln[:1], ln.split()[-1]
        target = os.path.realpath(os.path.join(root, name))
        if kind not in "-d" or os.path.commonpath([root, target]) != root:
            raise ValueError(f"refusing {name!r}")
    subprocess.run(["tar", "--zstd", "-xf", str(archive), "-C", str(dest),
                    "--no-same-owner", "--no-same-permissions"], check=True)
    return True


def fetch(url):
    """Download + verify + unpack. True when areas/earth is in place."""
    with tempfile.TemporaryDirectory(prefix="topopaper-starter-") as tmp:
        arc = Path(tmp) / os.path.basename(url.split("?")[0])
        log(f"downloading {url}")
        download(url, arc, "starter globe")
        sums = Path(tmp) / "sum.sha256"
        download(url + ".sha256", sums, "checksum", timeout=30)
        want = expected_sha(sums.read_text(errors="replace"))
        got = sha256(arc)
        if not want or want != got:
            raise ValueError(f"checksum mismatch (expected {want}, got {got})")
        log("checksum ok; unpacking")
        stage = Path(tempfile.mkdtemp(prefix=".starter-", dir=_ensure(paths.data_dir())))
        try:
            extract(arc, stage)
            src = stage / "areas" / "earth"
            if not (src / "terrain.bin").is_file():
                raise ValueError("archive has no areas/earth/terrain.bin")
            dst = paths.areas_dir() / "earth"
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                shutil.rmtree(dst)
            os.replace(src, dst)        # one rename: the engine never sees half a pack
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    return util.pack_exists("earth")


def _ensure(d):
    d.mkdir(parents=True, exist_ok=True)
    return d


def build():
    log("building the globe from elevation tiles (this takes a while)...")
    cmd, env = util.ctl_cmd("build", "--earth")
    return subprocess.run(cmd, env=env).returncode == 0 and util.pack_exists("earth")


def finish():
    if not util.current_area():
        util.fly("earth", quiet=True)
    util.notify("the globe is ready", 3000)


def main(argv=None):
    argv = list(argv or [])
    if "-h" in argv or "--help" in argv:
        print("usage: topopaper-ctl starter [--build] [--force] [--url URL]\n\n"
              "Download the prebuilt globe (or --build it locally) into "
              f"{paths.areas_dir() / 'earth'}")
        return 0
    url = URL
    if "--url" in argv:
        i = argv.index("--url")
        if i + 1 >= len(argv):
            print("--url needs a value", file=sys.stderr)
            return 2
        url = argv[i + 1]
    if util.pack_exists("earth") and "--force" not in argv:
        log("the globe is already installed (--force to fetch it again)")
        if not util.current_area():
            util.fly("earth", quiet=True)
        return 0
    if "--build" not in argv:
        net.prefer_ipv4()
        try:
            if fetch(url):
                finish()
                return 0
        except Exception as e:          # any failure: fall back to building it
            log(f"download failed: {e}")
            util.notify("couldn't download the globe; building it instead (slow)", 5000)
    if build():
        finish()
        return 0
    util.notify("couldn't get the globe; see `topopaper-ctl doctor`", 6000)
    return 1
