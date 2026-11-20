"""원본 PPT(templates/skmns_template.pptx)를 템플릿으로 값만 채운다.

템플릿 구조 (9월 보고서 기준):
 1 표지: 제목 / 고객사명 / 기간 텍스트
 2 목차
 3 장비현황: 요약표 2x2 (Device List (LBS) / VM 2 대), 장비표 (헤더 + 서버행)
 4 성능현황: 그룹명 텍스트 'LBS' + 성능표 (헤더 2행 + 서버행)
 5 백업현황: 표 (헤더 + 서버행)
 6 지원내역: 표 (헤더 + 행)
 7 감사합니다
행 수가 달라지면 마지막 데이터 행을 복제/삭제한다(서식 유지).
"""
from __future__ import annotations

import copy
from pathlib import Path

from pptx import Presentation
from pptx.oxml.ns import qn

from .config import project_path

SUPPORT_ROWS_PER_SLIDE = 12


# ---------- 셀/도형 텍스트 (서식 유지) ----------
def set_text(text_frame, text: str) -> None:
    """첫 run 의 서식을 유지하며 텍스트 교체. 빈 줄(여백용 문단)은 남긴다."""
    paras = text_frame.paragraphs
    target = next((p for p in paras if p.runs), paras[0])
    for p in paras:
        if p is target:
            continue
        for r in list(p.runs):
            r._r.getparent().remove(r._r)
    runs = target.runs
    if runs:
        runs[0].text = text
        for r in runs[1:]:
            r._r.getparent().remove(r._r)
    else:
        r = target.add_run()
        r.text = text
        end = target._p.find(qn("a:endParaRPr"))
        if end is not None:
            rpr = copy.deepcopy(end)
            rpr.tag = qn("a:rPr")
            old = r._r.find(qn("a:rPr"))
            if old is not None:
                r._r.remove(old)
            r._r.insert(0, rpr)


def set_cell(table, row: int, col: int, value) -> None:
    set_text(table.cell(row, col).text_frame, "" if value is None else str(value))


def resize_rows(table, header_rows: int, n: int) -> None:
    """데이터 행 수를 n 으로 맞춘다 (최소 1행 유지)."""
    n = max(n, 1)
    tbl = table._tbl
    trs = tbl.findall(qn("a:tr"))
    data = trs[header_rows:]
    while len(data) > n:
        tbl.remove(data.pop())
    while len(data) < n:
        new = copy.deepcopy(data[-1])
        data[-1].addnext(new)
        data.append(new)


def find_table(slide, header_text: str):
    for sh in slide.shapes:
        if sh.has_table and header_text in sh.table.cell(0, 0).text:
            return sh
    raise LookupError(f"표를 찾지 못함: {header_text}")


def find_text_shape(slide, contains: str):
    for sh in slide.shapes:
        if sh.has_text_frame and contains in sh.text_frame.text:
            return sh
    raise LookupError(f"텍스트를 찾지 못함: {contains}")


def duplicate_slide(prs, index: int):
    """index 슬라이드를 복제해 바로 뒤에 둔다."""
    src = prs.slides[index]
    new = prs.slides.add_slide(src.slide_layout)
    for ph in list(new.placeholders):
        ph._element.getparent().remove(ph._element)
    for el in src.shapes._spTree:
        if el.tag in (qn("p:nvGrpSpPr"), qn("p:grpSpPr")):
            continue
        new.shapes._spTree.append(copy.deepcopy(el))
    ids = prs.slides._sldIdLst
    moved = ids[-1]
    ids.remove(moved)
    ids.insert(index + 1, moved)
    return new


def fmt_num(v) -> str:
    """보고서 표기: 값 없음 '-', 0 은 '0', 그 외 소수 2자리."""
    if v is None:
        return "-"
    if float(v) == 0:
        return "0"
    return f"{float(v):.2f}"


# ---------- 렌더 ----------
def render(data: dict, ccfg: dict, out_path: Path) -> Path:
    prs = Presentation(str(project_path(ccfg["template"])))
    slides = list(prs.slides)
    y, m, last = data["year"], data["month"], data["last_day"]

    # 1 표지
    s1 = slides[0]
    set_text(find_text_shape(s1, "MSP").text_frame, ccfg["report_title"])
    set_text(_cover_name(s1), ccfg["name"])
    set_text(find_text_shape(s1, "년").text_frame, f"{y}년 {m}월 1일 – {m}월 {last}일")

    groups = data["groups"]

    # 3 장비현황
    s3 = slides[2]
    summ = find_table(s3, "구 분").table
    resize_rows(summ, 1, len(groups))
    for i, g in enumerate(groups, start=1):
        set_cell(summ, i, 0, f'Device List ({g["name"]})')
        counts = {}
        for d in data["devices"]:
            if d["group"] == g["name"]:
                counts[d["device_type"]] = counts.get(d["device_type"], 0) + 1
        set_cell(summ, i, 1, ", ".join(f"{k} {v} 대" for k, v in counts.items()))
    dev_t = find_table(s3, "구분").table
    devs = data["devices"]
    resize_rows(dev_t, 1, len(devs))
    for i, d in enumerate(devs, start=1):
        vals = [d["category"], d["device_type"], d["name"], d.get("os", "-"), d.get("public_ip", "-"),
                d.get("cpu", "-"), d.get("memory_gb", "-"), d.get("storage", "-")]
        for c, v in enumerate(vals):
            set_cell(dev_t, i, c, v)

    # 4 성능현황 (그룹별 슬라이드)
    perf_idx = 3
    for _ in groups[1:]:
        duplicate_slide(prs, perf_idx)
    slides = list(prs.slides)
    perf_by_name = {p["name"]: p for p in data["performance"]}
    for gi, g in enumerate(groups):
        s = slides[perf_idx + gi]
        # 복제된 슬라이드에도 템플릿의 그룹명이 그대로 있으므로 첫 그룹명으로 찾는다
        set_text(find_text_shape(s, groups[0]["name"]).text_frame, g["name"])
        t = find_table(s, "디바이스").table
        names = [d["name"] for d in devs if d["group"] == g["name"] and not d.get("missing")]
        resize_rows(t, 2, len(names))
        for i, name in enumerate(names, start=2):
            p = perf_by_name.get(name, {})
            vals = [name, p.get("cpu"), p.get("memory_gb"), p.get("storage"),
                    fmt_num(p.get("cpu_max")), fmt_num(p.get("cpu_avg")),
                    fmt_num(p.get("mem_max")), fmt_num(p.get("mem_avg")),
                    fmt_num(p.get("swap_max")), fmt_num(p.get("swap_avg")),
                    p.get("fs_name") or "-", fmt_num(p.get("disk_max")), fmt_num(p.get("disk_avg"))]
            for c, v in enumerate(vals):
                set_cell(t, i, c, v)
    shift = len(groups) - 1

    # 5 백업현황
    s5 = slides[4 + shift]
    bt = find_table(s5, "백업").table
    resize_rows(bt, 1, len(data["backup"]))
    for i, b in enumerate(data["backup"], start=1):
        for c, v in enumerate([b["server"], b["frequency"], b["policy"], b["result"]]):
            set_cell(bt, i, c, v)

    # 6 지원내역 (페이지 분할)
    sup_idx = 5 + shift
    sup = data["support"]
    pages = [sup[i:i + SUPPORT_ROWS_PER_SLIDE] for i in range(0, len(sup), SUPPORT_ROWS_PER_SLIDE)] or [[]]
    for _ in pages[1:]:
        duplicate_slide(prs, sup_idx)
    slides = list(prs.slides)
    for pi, page in enumerate(pages):
        t = find_table(slides[sup_idx + pi], "Date").table
        resize_rows(t, 1, len(page))
        if not page:
            for c in range(4):
                set_cell(t, 1, c, "")
        for i, e in enumerate(page, start=1):
            for c, v in enumerate([e["date"], e["kind"], e["content"], e["result"]]):
                set_cell(t, i, c, v)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out_path))
    return out_path


def _cover_name(slide):
    """표지 고객사명 텍스트 박스 (템플릿 순서상 제목 다음 텍스트박스)."""
    boxes = [sh for sh in slide.shapes if sh.has_text_frame and sh.text_frame.text.strip()]
    return boxes[1].text_frame
