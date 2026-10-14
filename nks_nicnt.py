"""Minimal NI .nicnt writer for third-party NKS libraries (classic browser)."""
from __future__ import annotations

import struct
import uuid
from pathlib import Path

NICNT_SIZE = 512512  # observed factory/third-party container size
HEADER = b"/\\ NI FC MTD  /\\"  # 16 bytes including spaces as in dumps


def build_product_hints_xml(
    *,
    name: str,
    company: str,
    upid: str | None = None,
    reg_key: str | None = None,
) -> str:
    upid = upid or str(uuid.uuid4())
    reg_key = reg_key or name
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="no" ?>
<ProductHints spec="1.0.16">

  <Product version="1">
    <UPID>{upid}</UPID>
    <Name>{name}</Name>
    <Type>Content</Type>
    <NKSEnabled>true</NKSEnabled>
    <Relevance>
      <Application minVersion="6.0.0.0" nativeContent="true">kontakt</Application>
      <Application minVersion="2.0.0.0" nativeContent="false">kkontrol</Application>
      <Application nativeContent="false">maschine</Application>
    </Relevance>
    <PoweredBy>Kontakt</PoweredBy>
    <Visibility target="Standalone">7</Visibility>
    <Company>{company}</Company>
    <AuthSystem>None</AuthSystem>
    <RegKey>{reg_key}</RegKey>
    <Icon>kontakt</Icon>
  </Product>

</ProductHints>
"""


def write_nicnt(
    dest: Path,
    *,
    name: str,
    company: str,
    upid: str | None = None,
) -> Path:
    """Write a 512512-byte .nicnt with ProductHints at offset 256."""
    xml = build_product_hints_xml(name=name, company=company, upid=upid).encode("utf-8")
    if len(xml) > 2000:
        raise ValueError("ProductHints XML too large for simple nicnt layout")

    buf = bytearray(NICNT_SIZE)
    # Header at 0
    buf[0:16] = HEADER
    # version-like utf16-ish "1.0.0" observed near offset 50-60 in some files — optional
    # XML at 256
    buf[256:256 + len(xml)] = xml
    # trailing marker near end (observed in Drop Squad)
    tail = b"NI FC MTD  /\\"
    buf[NICNT_SIZE - 253: NICNT_SIZE - 253 + len(tail)] = tail

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(bytes(buf))
    return dest
