# -*- coding: utf-8 -*-
"""在任意平台构建 sportwin 的 Debian amd64 安装包（仅用 Python 标准库）。"""
from __future__ import annotations

import gzip
import hashlib
import io
import re
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
VERSION = re.search(
    r'__version__\s*=\s*["\']([^"\']+)',
    (ROOT / "sportwin" / "__init__.py").read_text(encoding="utf-8"),
).group(1)
PACKAGE = "sportwin"
PREFIX = f"usr/lib/{PACKAGE}"


def tar_gz(entries: list[tuple[str, bytes, int]]) -> bytes:
    raw = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tar:
            # dpkg 不会为归档中的文件自动补齐父目录，必须显式写入目录条目。
            directories = set()
            for name, _, _ in entries:
                parent = name.rsplit("/", 1)[0] if "/" in name else "."
                while parent not in (".", "", "/"):
                    directories.add(parent)
                    parent = parent.rsplit("/", 1)[0] if "/" in parent else "."
            for name in sorted(directories, key=lambda value: (value.count("/"), value)):
                info = tarfile.TarInfo(name=name + "/")
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                tar.addfile(info)
            for name, payload, mode in entries:
                info = tarfile.TarInfo(name=name)
                info.size = len(payload)
                info.mode = mode
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                tar.addfile(info, io.BytesIO(payload))
    return raw.getvalue()


def ar_member(name: str, payload: bytes) -> bytes:
    header = (
        f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{0o100644:<8o}{len(payload):<10}`\n"
    ).encode("ascii")
    return header + payload + (b"\n" if len(payload) % 2 else b"")


def collect_files() -> list[tuple[str, bytes, int]]:
    files = [ROOT / "sport_run.py", ROOT / "README.md"]
    files += sorted((ROOT / "sportwin").rglob("*.py"))
    files += sorted((ROOT / "sportwin" / "assets").rglob("*"))
    files += sorted((ROOT / "data").rglob("*.json"))
    files += sorted((ROOT / "docs").rglob("*.md"))
    entries = []
    for path in files:
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        entries.append((f"./{PREFIX}/{rel}", path.read_bytes(), 0o644))

    launcher = b"#!/bin/sh\nexec /usr/bin/python3 /usr/lib/sportwin/sport_run.py \"$@\"\n"
    entries.append(("./usr/bin/sportwin", launcher, 0o755))
    desktop = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=一键跑步\n"
        "Comment=运动世界校园一键跑步\n"
        "Exec=sportwin\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
        "StartupNotify=true\n"
    ).encode("utf-8")
    entries.append(("./usr/share/applications/sportwin.desktop", desktop, 0o644))
    entries.append(("./usr/share/doc/sportwin/copyright", (ROOT / "LICENSE").read_bytes(), 0o644))
    return entries


def build() -> Path:
    data_entries = collect_files()
    installed_size = sum(len(payload) for _, payload, _ in data_entries) // 1024 + 1
    control = (
        f"Package: {PACKAGE}\n"
        f"Version: {VERSION}\n"
        "Section: utils\n"
        "Priority: optional\n"
        "Architecture: amd64\n"
        f"Installed-Size: {installed_size}\n"
        "Depends: python3 (>= 3.9), python3-tk\n"
        "Maintainer: Sportwin Contributors <noreply@localhost>\n"
        "Description: 运动世界校园一键跑步\n"
        " 提供图形界面与命令行入口，配置和日志保存在当前用户目录。\n"
    ).encode("utf-8")
    archive = (
        b"!<arch>\n"
        + ar_member("debian-binary", b"2.0\n")
        + ar_member("control.tar.gz", tar_gz([("./control", control, 0o644)]))
        + ar_member("data.tar.gz", tar_gz(data_entries))
    )
    DIST.mkdir(exist_ok=True)
    output = DIST / f"{PACKAGE}_{VERSION}_amd64.deb"
    output.write_bytes(archive)
    return output


def verify(path: Path) -> None:
    raw = path.read_bytes()
    assert raw.startswith(b"!<arch>\n"), "不是有效的 ar 归档"
    members = {}
    offset = 8
    while offset < len(raw):
        header = raw[offset:offset + 60]
        assert len(header) == 60 and header[58:60] == b"`\n", "ar 成员头损坏"
        name = header[:16].decode("ascii").strip().rstrip("/")
        size = int(header[48:58].decode("ascii").strip())
        start = offset + 60
        members[name] = raw[start:start + size]
        offset = start + size + (size % 2)
    assert list(members) == ["debian-binary", "control.tar.gz", "data.tar.gz"]
    assert members["debian-binary"] == b"2.0\n"

    with tarfile.open(fileobj=io.BytesIO(members["control.tar.gz"]), mode="r:gz") as tar:
        control = tar.extractfile("./control").read().decode("utf-8")
    assert "Architecture: amd64\n" in control
    assert "Depends: python3 (>= 3.9), python3-tk\n" in control

    with tarfile.open(fileobj=io.BytesIO(members["data.tar.gz"]), mode="r:gz") as tar:
        data_members = {item.name: item for item in tar.getmembers()}
        required = {
            "./usr/",
            "./usr/bin/",
            "./usr/lib/",
            f"./{PREFIX}/",
            f"./{PREFIX}/sportwin/",
            "./usr/bin/sportwin",
            "./usr/share/applications/sportwin.desktop",
            f"./{PREFIX}/sport_run.py",
            f"./{PREFIX}/sportwin/assets/gt4.js",
            f"./{PREFIX}/data/sample_trajectory_plain.json",
        }
        assert required <= set(data_members), f"缺少文件：{required - set(data_members)}"
        assert data_members["./usr/bin/sportwin"].mode == 0o755
        assert all(item.uid == 0 and item.gid == 0 for item in data_members.values())
        forbidden = ("credentials.bin", "config.json", "state.json", "logs/", "__pycache__")
        assert not any(any(part in name for part in forbidden) for name in data_members)
    print(f"DEB 校验通过：{len(data_members)} 个文件，amd64，未包含本机配置/凭据/日志。")


if __name__ == "__main__":
    result = build()
    verify(result)
    digest = hashlib.sha256(result.read_bytes()).hexdigest()
    print(f"已生成：{result}")
    print(f"大小：{result.stat().st_size} bytes")
    print(f"SHA256：{digest}")
