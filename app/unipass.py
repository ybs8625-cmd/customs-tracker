"""UNI-PASS 화물통관진행정보 OpenAPI 클라이언트."""

from __future__ import annotations

import asyncio
import os
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import httpx

UNIPASS_URL = (
    "https://unipass.customs.go.kr:38010/ext/rest/"
    "cargCsclPrgsInfoQry/retrieveCargCsclPrgsInfo"
)
_MAX_ATTEMPTS = 3
_RETRY_DELAYS_SEC = (1.0, 2.0)

# 특송·일반 공통 진행 단계 (표시용)
# 일반: 수입신고 → 수입신고수리 → 물품반출
# 특송: 통관특례신청완료 → 반입신고 → 반출신고
STAGE_FLOW = [
    "입항적재화물목록 제출",
    "입항적재화물목록 심사완료",
    "입항보고 수리",
    "하선신고 수리",
    "통관목록접수",
    "통관특례·수입신고",
    "반입·수입신고수리",
    "물품반출",
]

# 이벤트/상태 문자열 → STAGE_FLOW index
_STAGE_ALIASES: list[tuple[str, int]] = [
    ("물품반출", 7),
    ("반출신고", 7),
    ("반출", 7),
    ("수입신고수리", 6),
    ("반입신고", 6),
    ("반입", 6),
    ("통관특례", 5),
    ("수입신고", 5),
    ("통관목록", 4),
    ("목록통관", 4),
    ("하선", 3),
    ("입항보고", 2),
    ("심사완료", 1),
    ("목록 제출", 0),
    ("적재화물목록 제출", 0),
    ("적하목록", 0),
]


@dataclass
class ProgressEvent:
    stage: str
    warehouse: str = ""
    processed_at: str = ""
    packs: str = ""
    weight: str = ""
    declaration_no: str = ""
    note: str = ""


@dataclass
class CargoTrack:
    found: bool
    hbl: str = ""
    mbl: str = ""
    cargo_no: str = ""
    year: int = 0
    status: str = ""
    status_detail: str = ""
    product_name: str = ""
    arrival_date: str = ""
    arrival_port: str = ""
    customs: str = ""
    carrier: str = ""
    forwarder: str = ""
    packs: str = ""
    weight: str = ""
    vessel: str = ""
    container: str = ""
    cargo_type: str = ""
    processed_at: str = ""
    events: list[ProgressEvent] = field(default_factory=list)
    current_stage_index: int = -1
    stages: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    source: str = "unipass"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data


def _text(node: ET.Element | None, tag: str, default: str = "") -> str:
    if node is None:
        return default
    child = node.find(tag)
    if child is None or child.text is None:
        return default
    return child.text.strip()


def _stage_index_for_text(text: str) -> int:
    raw = (text or "").strip()
    if not raw:
        return -1
    best = -1
    for i, stage in enumerate(STAGE_FLOW):
        if stage in raw or raw in stage:
            best = max(best, i)
    for key, idx in _STAGE_ALIASES:
        if key in raw:
            best = max(best, idx)
    return best


def _match_stage_index(status: str, events: list[ProgressEvent]) -> int:
    best = _stage_index_for_text(status)
    for ev in events:
        best = max(best, _stage_index_for_text(ev.stage))
    return best


def _display_name_for_event(stage_idx: int, ev: ProgressEvent | None) -> str:
    """특송 실이벤트명이 있으면 그대로 보여 주고, 없으면 공통 단계명."""
    if ev and (ev.stage or "").strip():
        return ev.stage.strip()
    if 0 <= stage_idx < len(STAGE_FLOW):
        return STAGE_FLOW[stage_idx]
    return "-"


def _build_stages(current_index: int, events: list[ProgressEvent]) -> list[dict[str, Any]]:
    event_by_index: dict[int, ProgressEvent] = {}
    for ev in events:
        idx = _stage_index_for_text(ev.stage)
        if idx < 0:
            continue
        prev = event_by_index.get(idx)
        # 같은 단계는 최초 시각 유지
        if prev is None or (ev.processed_at or "") < (prev.processed_at or ""):
            event_by_index[idx] = ev

    stages: list[dict[str, Any]] = []
    for i, name in enumerate(STAGE_FLOW):
        if current_index < 0:
            state = "pending"
        elif i < current_index:
            state = "done"
        elif i == current_index:
            state = "current"
        else:
            state = "pending"
        ev = event_by_index.get(i)
        stages.append(
            {
                "name": _display_name_for_event(i, ev) if ev else name,
                "state": state,
                "processed_at": ev.processed_at if ev else "",
                "warehouse": ev.warehouse if ev else "",
            }
        )
    return stages


def parse_unipass_xml(xml_text: str, hbl: str, year: int) -> CargoTrack:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return CargoTrack(found=False, hbl=hbl, year=year, error=f"XML 파싱 실패: {exc}")

    # 오류 응답
    err_msg = _text(root, "ntceInfo") or _text(root.find("ntceInfo"), "ntceInfo")
    # 일부 응답은 tCnt=0
    t_cnt = _text(root, "tCnt", "0")
    info = root.find("cargCsclPrgsInfoQryVo")
    if info is None:
        # 복수 결과가 list 로 올 수도 있음
        infos = root.findall(".//cargCsclPrgsInfoQryVo")
        info = infos[0] if infos else None

    if info is None:
        msg = err_msg or "조회 결과가 없습니다. HBL/연도를 확인하세요."
        if "인증키" in xml_text or "API" in xml_text or "크키" in xml_text:
            msg = "유니패스 API 인증키가 없거나 잘못되었습니다."
        return CargoTrack(found=False, hbl=hbl, year=year, error=msg)

    events: list[ProgressEvent] = []
    for dtl in root.findall(".//cargCsclPrgsInfoDtlQryVo"):
        stage = (
            _text(dtl, "cargTrcnRelaBsopTpcd")
            or _text(dtl, "cargTrcnRelrSttnCag")
            or _text(dtl, "prcsSttsNm")
            or _text(dtl, "csclPrgsStts")
            or _text(dtl, "rlbrCn")
            or "처리"
        )
        events.append(
            ProgressEvent(
                stage=stage,
                warehouse=_text(dtl, "shedNm") or _text(dtl, "shedSgn"),
                processed_at=_text(dtl, "prcsDttm") or _text(dtl, "rlbrDttm"),
                packs=_text(dtl, "pckGcnt"),
                weight=_text(dtl, "wght"),
                declaration_no=_text(dtl, "dclrNo") or _text(dtl, "rlbrBssNo"),
                note=_text(dtl, "rlbrCn") if stage != _text(dtl, "rlbrCn") else "",
            )
        )

    # 이벤트 순서: 시간 → 단계 index (같은 시각이면 더 진행된 단계가 최신)
    def sort_key(ev: ProgressEvent) -> tuple[str, int]:
        return (ev.processed_at or "", _stage_index_for_text(ev.stage))

    events_sorted = sorted(events, key=sort_key)

    status = _text(info, "csclPrgsStts") or _text(info, "prgsStts")
    status_detail = _text(info, "prgsStts") or status
    current_idx = _match_stage_index(status, events_sorted)
    processed_at = _text(info, "prcsDttm")

    # 헤더 상태가 늦어도(특송 반출신고 등) 최신 이벤트 기준으로 표시/알림
    if events_sorted:
        newest = events_sorted[-1]
        newest_idx = _stage_index_for_text(newest.stage)
        if newest_idx > _stage_index_for_text(status):
            status = newest.stage
            current_idx = max(current_idx, newest_idx)
            if newest.processed_at:
                processed_at = newest.processed_at

    track = CargoTrack(
        found=True,
        hbl=_text(info, "hblNo", hbl),
        mbl=_text(info, "mblNo"),
        cargo_no=_text(info, "cargMtNo"),
        year=year,
        status=status,
        status_detail=status_detail,
        product_name=_text(info, "prnm"),
        arrival_date=_text(info, "etprDt"),
        arrival_port=_text(info, "dsprNm") or _text(info, "dsprCd"),
        customs=_text(info, "etprCstm"),
        carrier=_text(info, "shcoFlco"),
        forwarder=_text(info, "frwrEntsConm"),
        packs=_text(info, "pckGcnt") + (" " + _text(info, "pckUt") if _text(info, "pckUt") else ""),
        weight=_text(info, "ttwg") + (" " + _text(info, "wghtUt") if _text(info, "wghtUt") else ""),
        vessel=_text(info, "shipNm"),
        container=_text(info, "cntrNo"),
        cargo_type=_text(info, "cargTp"),
        processed_at=processed_at,
        events=list(reversed(events_sorted)),  # 최신 먼저
        current_stage_index=current_idx,
        stages=_build_stages(current_idx, events_sorted),
        source="unipass",
    )
    # tCnt 참고용
    _ = t_cnt
    return track


def demo_cargo(hbl: str, year: int) -> CargoTrack | None:
    """API 키 없을 때 샘플 송장(직전 조회분)으로 UI 확인용."""
    if hbl != "509799520393":
        return None
    events = [
        ProgressEvent("통관목록접수", "인천세관 지정장치장(특송물류센터)", "2026-08-05 12:37:21", "1", "1 KG", "SE02072600000299"),
        ProgressEvent("하선신고 수리", "인천세관 지정장치장(특송물류센터)", "2026-08-05 10:00:15", "1 GT", "1 KG", "26020108503", "하선반입기한:2026-08-12"),
        ProgressEvent("입항적재화물목록 운항정보 정정", "", "2026-08-05 08:24:52", "", "KG", "26WDFCF289I00000001"),
        ProgressEvent("입항보고 수리", "", "2026-08-05 08:24:52", "", "KG", "26WDFCF289I"),
        ProgressEvent("입항적재화물목록 심사완료", "인천세관 지정장치장(특송물류센터)", "2026-08-04 23:34:40", "1 GT", "1 KG", "", "주식회사 윈핸드해운항공"),
        ProgressEvent("입항적재화물목록 제출", "인천세관 지정장치장(특송물류센터)", "2026-08-04 22:34:34", "1 GT", "1 KG"),
    ]
    idx = _match_stage_index("통관목록접수", events)
    return CargoTrack(
        found=True,
        hbl=hbl,
        mbl="WDFCGBF32894656",
        cargo_no="26WDFCF289i-0904-1013",
        year=year,
        status="통관목록접수",
        status_detail="통관목록접수",
        product_name="X2 MINI GAMING DEVICE",
        arrival_date="20260805",
        arrival_port="인천항",
        customs="인천세관",
        carrier="(주)위동해운",
        forwarder="주식회사 윈핸드해운항공",
        packs="1 GT",
        weight="1 KG",
        vessel="NEWGOLDENBRIDGE 5",
        container="WDFU7001711",
        cargo_type="수입 일반화물",
        processed_at="2026-08-05 12:37:21",
        events=events,
        current_stage_index=idx,
        stages=_build_stages(idx, list(reversed(events))),
        source="demo",
    )


async def fetch_cargo(hbl: str, year: int | None = None, api_key: str | None = None) -> CargoTrack:
    hbl = hbl.strip().replace(" ", "")
    year = year or datetime.now().year
    api_key = (api_key or os.getenv("UNIPASS_API_KEY", "")).strip()

    if not hbl:
        return CargoTrack(found=False, error="HBL(송장번호)를 입력하세요.")

    if not api_key:
        demo = demo_cargo(hbl, year)
        if demo:
            demo.error = (
                "데모 데이터입니다. 실시간 조회를 원하면 .env에 UNIPASS_API_KEY를 설정하세요."
            )
            return demo
        return CargoTrack(
            found=False,
            hbl=hbl,
            year=year,
            error=(
                "UNIPASS_API_KEY가 없습니다. "
                "유니패스 로그인 → My메뉴 → 서비스관리 → OpenAPI 사용관리에서 "
                "'화물통관진행정보' API를 신청한 뒤 .env에 넣어주세요.\n"
                "샘플로 509799520393 은 API 키 없이 데모 조회됩니다."
            ),
        )

    params = {"crkyCn": api_key, "hblNo": hbl, "blYy": str(year)}
    url = f"{UNIPASS_URL}?{urlencode(params)}"

    text = ""
    last_error = ""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=20.0, verify=True) as client:
                resp = await client.get(url, headers={"Accept": "application/xml"})
                resp.raise_for_status()
                text = resp.text
            break
        except httpx.HTTPError as exc:
            detail = str(exc).strip() or type(exc).__name__
            cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
            if cause is not None:
                cause_text = str(cause).strip() or type(cause).__name__
                detail = f"{detail} (cause={cause_text})"
            last_error = f"유니패스 연결 실패: {detail}"
            transient = isinstance(
                exc,
                (
                    httpx.ConnectError,
                    httpx.ConnectTimeout,
                    httpx.ReadTimeout,
                    httpx.WriteTimeout,
                    httpx.PoolTimeout,
                    httpx.RemoteProtocolError,
                    httpx.ReadError,
                    httpx.WriteError,
                ),
            ) or (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code in {408, 425, 429, 500, 502, 503, 504}
            )
            if attempt < _MAX_ATTEMPTS and transient:
                await asyncio.sleep(
                    _RETRY_DELAYS_SEC[min(attempt - 1, len(_RETRY_DELAYS_SEC) - 1)]
                )
                continue
            return CargoTrack(found=False, hbl=hbl, year=year, error=last_error)
    else:
        return CargoTrack(
            found=False,
            hbl=hbl,
            year=year,
            error=last_error or "유니패스 연결 실패",
        )

    track = parse_unipass_xml(text, hbl=hbl, year=year)

    # 해당 연도 없으면 전년도도 시도
    if not track.found and year == datetime.now().year:
        prev = await fetch_cargo(hbl, year=year - 1, api_key=api_key)
        if prev.found:
            return prev
    return track
