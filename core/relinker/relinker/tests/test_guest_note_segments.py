from pathlib import Path
import os
import struct
import subprocess
import sys
import tempfile

from test_guest_intel_trampolines import PLAIN_SITE, guest_fixture, main_fixture


def main():
    relinker = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="anyps5-guest-notes-") as directory:
        root = Path(directory)
        for windows in (False, True):
            for kind in (4, 1, 2, 7):
                work = root / f"{windows}-{kind}"
                modules = work / "sce_module"
                modules.mkdir(parents=True)
                image = guest_fixture(PLAIN_SITE)
                phoff, = struct.unpack_from("<Q", image, 32)
                phsize, phcount = struct.unpack_from("<HH", image, 54)
                struct.pack_into("<H", image, 56, phcount + 1)
                struct.pack_into("<IIQQQQQQ", image, phoff + phcount * phsize,
                                 kind, 4, len(image) + 256, 0x4000, 0x4000, 24, 24, 1)
                (modules / "fixture.prx").write_bytes(image)
                source = work / "input.elf"
                source.write_bytes(main_fixture())
                output = work / ("output.exe" if windows else "output.elf")
                result = subprocess.run([str(relinker), *(["--windows"] if windows else []),
                                         str(source), str(output)],
                                        capture_output=True, text=True, timeout=30)
                if kind != 4:
                    assert result.returncode == 2 and "ELF range exceeds file" in result.stderr, result
                    assert not output.exists()
                    continue
                assert result.returncode == 0, (result.stdout, result.stderr)
                guest = work / "app0" / "sce_module" / "fixture.prx.guest.prx"
                assert guest.read_bytes().startswith(b"MZ" if windows else b"\x7fELF")
                if windows == (os.name == "nt"):
                    if not windows:
                        output.chmod(0o755)
                    run = subprocess.run([str(output)], capture_output=True, timeout=30)
                    assert run.returncode == 42, (run.returncode, run.stdout, run.stderr)
    print("Guest note segment tests passed")


if __name__ == "__main__":
    main()
