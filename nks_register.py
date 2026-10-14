"""Kontakt + Komplete Kontrol registration helpers for NKS Desktop Builder."""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import uuid
from pathlib import Path
from typing import Callable
from nks_nicnt import write_nicnt


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path

def sanitize_segment(name: str, fallback: str = "Item") -> str:
    s = re.sub(r"[^A-Za-z0-9._\- ]+", "", name or "").strip(" ._")
    return s or fallback


def _kontakt_user_config_dbs() -> list[Path]:
    home = Path.home()
    return [
        home / "Library/Application Support/Native Instruments/Kontakt 8/user_config.db3",
        home / "Library/Application Support/Native Instruments/Kontakt 7/user_config.db3",
        home
        / "Library/Application Support/Native Instruments/Komplete Kontrol/Browser Data/user_config.db3",
    ]


def register_user_library_path(library_root: Path, alias: str) -> list[str]:
    notes: list[str] = []
    root = str(library_root.expanduser().resolve())
    for db_path in _kontakt_user_config_dbs():
        if not db_path.is_file():
            continue
        try:
            con = sqlite3.connect(str(db_path))
            cur = con.cursor()
            cur.execute(
                "SELECT path FROM k_content_path WHERE path=? OR alias=?",
                (root, alias),
            )
            row = cur.fetchone()
            if row:
                cur.execute(
                    "UPDATE k_content_path SET path=?, alias=?, content_type=3 WHERE alias=? OR path=?",
                    (root, alias, alias, root),
                )
                notes.append(f"updated {db_path}")
            else:
                cur.execute(
                    "INSERT INTO k_content_path(path, content_type, alias) VALUES (?,?,?)",
                    (root, 3, alias),
                )
                notes.append(f"inserted {db_path}")
            con.commit()
            con.close()
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{db_path.name}: {exc}")
    return notes


def write_service_center_product(vendor: str, library: str) -> Path:
    sc_dir = Path("/Library/Application Support/Native Instruments/Service Center")
    user_sc = Path.home() / "Library/Application Support/Native Instruments/Service Center"
    dest_dir = sc_dir if os.access(sc_dir, os.W_OK) else user_sc
    ensure_dir(dest_dir)
    upid = str(uuid.uuid4())
    dest = dest_dir / f"{library}.xml"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ProductHints>
  <Product version="1">
    <UPID>{upid}</UPID>
    <Name>{library}</Name>
    <Type>Content</Type>
    <RegKey>{library}</RegKey>
    <AuthSystem>None</AuthSystem>
    <Company>{vendor}</Company>
    <PoweredBy>Kontakt</PoweredBy>
    <NKSEnabled>true</NKSEnabled>
    <Icon>kontakt</Icon>
    <Relevance>
      <Application minVersion="6" nativeContent="true">Kontakt</Application>
      <Application minVersion="2" nativeContent="false">KKontrol</Application>
      <Application nativeContent="false">Maschine</Application>
    </Relevance>
    <Visibility>0x07</Visibility>
  </Product>
</ProductHints>
"""
    dest.write_text(xml, encoding="utf-8")
    return dest


def rebuild_quickload_aliases(source_library: Path, vendor: str, library: str) -> Path:
    import shutil

    ql_instr = (
        Path.home()
        / "Library/Application Support/Native Instruments/Kontakt 8/QuickLoad/Instr"
    )
    ensure_dir(ql_instr)
    for bad in ("Silence Other Sounds", "Vendor", "V"):
        bad_path = ql_instr / bad
        if bad_path.exists():
            shutil.rmtree(bad_path, ignore_errors=True)

    dest_root = ensure_dir(ql_instr / vendor / library)
    candidates = [
        source_library / "Instruments",
        source_library / "Kontakt Instruments",
        source_library / "KONTAKT Instruments",
        source_library / "KONTAKT_Instruments",
    ]
    src_instr = next((p for p in candidates if p.is_dir()), None)
    if src_instr is None:
        return dest_root

    dest_instr = dest_root / src_instr.name
    if dest_instr.is_symlink() or dest_instr.exists():
        if dest_instr.is_symlink() or dest_instr.is_file():
            dest_instr.unlink()
        elif dest_instr.is_dir():
            shutil.rmtree(dest_instr, ignore_errors=True)
    dest_instr.symlink_to(src_instr, target_is_directory=True)
    return dest_root



def install_library_for_kontakt_kk(
    *,
    vendor: str,
    library: str,
    source_path: Path,
    log: Callable[[str], None] | None = None,
) -> list[str]:
    notes: list[str] = []

    def _log(msg: str) -> None:
        notes.append(msg)
        if log:
            log(msg)

    try:
        root = source_path.expanduser().resolve()
    except OSError:
        root = source_path.expanduser()

    try:
        nicnt_path = root / f"{library}.nicnt"
        write_nicnt(nicnt_path, name=library, company=vendor)
        _log(f"nicnt → {nicnt_path}")
    except Exception as exc:  # noqa: BLE001
        _log(f"nicnt write failed: {exc}")

    for n in register_user_library_path(root, library):
        _log(f"user_config: {n}")

    try:
        sc = write_service_center_product(vendor, library)
        _log(f"Service Center product → {sc}")
    except Exception as exc:  # noqa: BLE001
        _log(f"Service Center write failed: {exc}")

    try:
        ql = rebuild_quickload_aliases(root, vendor, library)
        _log(f"QuickLoad rebuilt → {ql}")
    except Exception as exc:  # noqa: BLE001
        _log(f"QuickLoad rebuild failed: {exc}")

    return notes
