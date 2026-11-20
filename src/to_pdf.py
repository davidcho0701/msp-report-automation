"""PPTX -> PDF. Windows 에서는 설치된 PowerPoint(COM), 없으면 LibreOffice headless."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

PP_SAVE_AS_PDF = 32


def _powerpoint(src: Path, dst: Path) -> None:
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    app = None
    try:
        app = win32com.client.DispatchEx("PowerPoint.Application")
        pres = app.Presentations.Open(str(src), ReadOnly=True, Untitled=False, WithWindow=False)
        try:
            pres.SaveAs(str(dst), PP_SAVE_AS_PDF)
        finally:
            pres.Close()
    finally:
        if app is not None:
            try:
                if app.Presentations.Count == 0:
                    app.Quit()
            except Exception:  # noqa: BLE001
                pass
        pythoncom.CoUninitialize()


def _libreoffice(src: Path, dst: Path) -> None:
    exe = shutil.which("soffice") or r"C:\Program Files\LibreOffice\program\soffice.exe"
    if not Path(exe).exists():
        raise RuntimeError("PowerPoint/LibreOffice 를 찾지 못해 PDF 변환 불가")
    subprocess.run([exe, "--headless", "--convert-to", "pdf", "--outdir", str(dst.parent), str(src)],
                   check=True, capture_output=True, timeout=180)
    produced = dst.parent / (src.stem + ".pdf")
    if produced != dst:
        produced.replace(dst)


def convert(src: Path, dst: Path | None = None) -> Path:
    src = src.resolve()
    dst = (dst or src.with_suffix(".pdf")).resolve()
    if dst.exists():
        dst.unlink()
    if sys.platform == "win32":
        try:
            _powerpoint(src, dst)
        except ImportError:
            _libreoffice(src, dst)
    else:
        _libreoffice(src, dst)
    if not dst.exists():
        raise RuntimeError("PDF 파일이 생성되지 않았습니다")
    return dst
