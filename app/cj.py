"""CJ대한통운 배송조회.

네이버페이/네이버 검색과 동일한 상세 스캔을 쓰기 위해
trace.cjlogistics.com/next API를 우선 조회하고,
실패 시 www.cjlogistics.com 웹조회를 폴백한다.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

# 네이버가 링크하는 CJ 신규 추적 (상세 스캔 전부 포함)
TRACE_PAGE = "https://trace.cjlogistics.com/next/tracking.html"
TRACE_WAYBILL = "https://trace.cjlogistics.com/next/rest/selectTrackingWaybil.do"
TRACE_DETAIL = "https://trace.cjlogistics.com/next/rest/selectTrackingDetailList.do"

# 구 웹조회 폴백
TRACKING_PAGE = "https://www.cjlogistics.com/ko/tool/parcel/tracking"
TRACKING_DETAIL = "https://www.cjlogistics.com/ko/tool/parcel/tracking-detail"

_MAX_ATTEMPTS = 4
_RETRY_DELAYS_SEC = (0.8, 1.5, 3.0)

# 표시용 국내배송 단계
STAGE_FLOW = [
    "배송준비",
    "집화",
    "행낭포장",
    "이동중",
    "배송중",
    "배송완료",
]

# CJ 코드 -> 큰 단계(집화/이동중…) — 코드는 API마다 다를 수 있어 이름 매핑 우선
STATUS_BY_CODE = {
    "01": "배송준비",
    "11": "집화",
    "12": "집화",
    "R1": "행낭포장",
    "r1": "행낭포장",
    "21": "이동중",
    "41": "이동중",
    "42": "이동중",
    "43": "이동중",
    "44": "이동중",
    "82": "배송중",
    "84": "배송중",
    "91": "배송완료",
}

# 코드 폴백 라벨 (next API는 crgStDnm을 반드시 우선)
SCAN_LABEL_BY_CODE = {
    "01": "배송준비",
    "11": "집화처리",
    "12": "집화처리",
    "R1": "행낭포장",
    "21": "SM입고",
    "41": "간선상차",  # next API 기준 (www 구API는 44=상차)
    "42": "간선하차",
    "43": "간선이동중",
    "44": "간선상차",
    "82": "배송출발",
    "84": "배송중",
    "91": "배송완료",
}

# 같은 시각일 때 스캔명 순서 (네이버 타임라인과 동일하게 하차→상차)
SCAN_NAME_ORDER = {
    "배송준비": 1,
    "집화처리": 2,
    "행낭포장": 3,
    "SM입고": 4,
    "간선하차": 5,
    "간선이동중": 6,
    "간선상차": 7,
    "배송출발": 8,
    "배송중": 9,
    "배송완료": 10,
}

# 코드 폴백 순서
SCAN_ORDER = {
    "01": 1,
    "11": 2,
    "12": 2,
    "R1": 3,
    "21": 4,
    "42": 5,
    "43": 6,
    "41": 7,
    "44": 7,
    "82": 8,
    "84": 9,
    "91": 10,
}

STAGE_INDEX = {name: i for i, name in enumerate(STAGE_FLOW)}


@dataclass
class CjEvent:
    stage: str
    processed_at: str = ""
    location: str = ""
    status_code: str = ""
    raw_status: str = ""
    note: str = ""


@dataclass
class CjTrack:
    found: bool
    invoice: str = ""
    status: str = ""
    processed_at: str = ""
    location: str = ""
    events: list[CjEvent] = field(default_factory=list)
    current_stage_index: int = -1
    stages: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    source: str = "cj"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize_invoice(invoice: str) -> str:
    return re.sub(r"\D", "", (invoice or "").strip())


def _norm_code(code: str) -> str:
    return (code or "").strip().upper()


def _scan_label(code: str, scan_nm: str = "") -> str:
    """상세 표기명: 스캔명 우선, 없으면 코드 라벨."""
    text = (scan_nm or "").strip()
    if text:
        return text
    key = _norm_code(code)
    return SCAN_LABEL_BY_CODE.get(key) or SCAN_LABEL_BY_CODE.get(code) or key or "처리"


def _map_status(code: str, scan_nm: str) -> str:
    text = (scan_nm or "").strip()
    # 스캔명 우선 (next API에서 코드 41이 간선상차인 경우 등)
    for name in STAGE_FLOW:
        if name in text:
            return name
    aliases = (
        ("배달완료", "배송완료"),
        ("배송완료", "배송완료"),
        ("배송출발", "배송중"),
        ("배달출발", "배송중"),
        ("배송중", "배송중"),
        ("행낭포장", "행낭포장"),
        ("행낭", "행낭포장"),
        ("간선상차", "이동중"),
        ("간선하차", "이동중"),
        ("간선이동", "이동중"),
        ("간선", "이동중"),
        ("SM입고", "이동중"),
        ("이동", "이동중"),
        ("도착", "이동중"),
        ("집화", "집화"),
        ("인수", "집화"),
        ("접수", "배송준비"),
        ("준비", "배송준비"),
    )
    for key, mapped in aliases:
        if key in text:
            return mapped
    code_key = _norm_code(code)
    if code_key in STATUS_BY_CODE:
        return STATUS_BY_CODE[code_key]
    if code in STATUS_BY_CODE:
        return STATUS_BY_CODE[code]
    return text or "배송준비"


def _event_sort_key(ev: CjEvent) -> tuple:
    name = (ev.raw_status or "").strip()
    code = _norm_code(ev.status_code)
    return (
        ev.processed_at or "",
        SCAN_NAME_ORDER.get(name, SCAN_ORDER.get(code, 50)),
        STAGE_INDEX.get(ev.stage, -1),
    )


def _build_stages(current_index: int, events: list[CjEvent]) -> list[dict[str, Any]]:
    event_by_stage: dict[str, CjEvent] = {}
    for ev in events:
        idx = STAGE_INDEX.get(ev.stage)
        if idx is None:
            continue
        if ev.stage not in event_by_stage:
            event_by_stage[ev.stage] = ev

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
        ev = event_by_stage.get(name)
        stages.append(
            {
                "name": name,
                "state": state,
                "processed_at": ev.processed_at if ev else "",
                "location": ev.location if ev else "",
            }
        )
    return stages


def _extract_csrf(html: str) -> str | None:
    m = re.search(r'name="_csrf"\s+value="([^"]+)"', html)
    if m:
        return m.group(1)
    m = re.search(r'content="([^"]+)"\s+name="_csrf"', html)
    if m:
        return m.group(1)
    m = re.search(r'"csrf"\s*:\s*"([^"]+)"', html)
    return m.group(1) if m else None


def _format_http_error(exc: BaseException) -> str:
    parts = [type(exc).__name__]
    text = str(exc).strip()
    if text:
        parts.append(text)
    cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    if cause is not None:
        cause_text = str(cause).strip() or type(cause).__name__
        parts.append(f"cause={cause_text}")
    return ": ".join(parts) if len(parts) > 1 else parts[0]


def _is_transient_error(exc: BaseException) -> bool:
    if isinstance(
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
    ):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {408, 425, 429, 500, 502, 503, 504}
    return False


def _finalize_events(
    inv: str,
    events: list[CjEvent],
    *,
    source: str,
) -> CjTrack:
    if not events:
        return CjTrack(
            found=True,
            invoice=inv,
            status="배송준비",
            processed_at="",
            location="",
            events=[],
            current_stage_index=0,
            stages=_build_stages(0, []),
            error="운송장이 아직 등록되지 않았거나 배송준비 중입니다.",
            source=source,
        )

    events.sort(key=_event_sort_key)
    latest = events[-1]
    current_idx = max(
        (STAGE_INDEX[ev.stage] for ev in events if ev.stage in STAGE_INDEX),
        default=0,
    )
    # 네이버와 동일: 현재상태 = 최신 상세 스캔명
    status = latest.raw_status or latest.stage
    stage_for_idx = (
        latest.stage
        if latest.stage in STAGE_INDEX
        else _map_status(latest.status_code, latest.raw_status)
    )
    if stage_for_idx in STAGE_INDEX:
        current_idx = max(current_idx, STAGE_INDEX[stage_for_idx])

    return CjTrack(
        found=True,
        invoice=inv,
        status=status,
        processed_at=latest.processed_at,
        location=latest.location,
        events=list(reversed(events)),  # 최신 먼저
        current_stage_index=current_idx,
        stages=_build_stages(current_idx, events),
        source=source,
    )


async def _fetch_cj_next_once(inv: str) -> CjTrack:
    """네이버가 쓰는 CJ next 추적 API (상세 스캔 전체)."""
    timeout = httpx.Timeout(30.0, connect=20.0)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Origin": "https://trace.cjlogistics.com",
        "Referer": f"{TRACE_PAGE}?wblNo={inv}",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }

    async with httpx.AsyncClient(
        timeout=timeout,
        headers={
            "User-Agent": headers["User-Agent"],
            "Accept-Language": headers["Accept-Language"],
        },
        follow_redirects=True,
        http2=False,
    ) as client:
        # 세션/쿠키
        await client.get(f"{TRACE_PAGE}?wblNo={inv}")
        detail = await client.post(
            TRACE_DETAIL,
            data={"wblNo": inv},
            headers=headers,
        )
        detail.raise_for_status()
        payload = detail.json()

    if int(payload.get("resultCode") or 0) != 200:
        return CjTrack(
            found=False,
            invoice=inv,
            error=f"CJ next 조회 실패: {payload.get('resultMessage') or payload.get('resultCode')}",
            source="cj-next",
        )

    data = payload.get("data") or {}
    rows = data.get("svcOutList") or []
    if not rows:
        return CjTrack(
            found=True,
            invoice=inv,
            status="배송준비",
            events=[],
            current_stage_index=0,
            stages=_build_stages(0, []),
            error="운송장이 아직 등록되지 않았거나 배송준비 중입니다.",
            source="cj-next",
        )

    events: list[CjEvent] = []
    for row in rows:
        code = str(row.get("crgStDcd") or "")
        scan = str(row.get("crgStDnm") or "")
        label = _scan_label(code, scan)
        stage = _map_status(code, label)
        work_dt = str(row.get("workDt") or "").strip()
        work_hms = str(row.get("workHms") or "").strip()
        processed_at = f"{work_dt} {work_hms}".strip()
        events.append(
            CjEvent(
                stage=stage,
                processed_at=processed_at,
                location=str(row.get("branNm") or "").strip(),
                status_code=_norm_code(code) or code,
                raw_status=label,
                note=str(row.get("crgStDcdVal") or "").strip(),
            )
        )

    return _finalize_events(inv, events, source="cj-next")


async def _fetch_cj_www_once(inv: str) -> CjTrack:
    """구 www 웹조회 폴백 (상세가 줄어든 경우가 많음)."""
    timeout = httpx.Timeout(30.0, connect=20.0)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    }

    async with httpx.AsyncClient(
        timeout=timeout,
        headers=headers,
        follow_redirects=True,
        http2=False,
    ) as client:
        page = await client.get(TRACKING_PAGE)
        page.raise_for_status()
        csrf = _extract_csrf(page.text)
        if not csrf:
            return CjTrack(
                found=False,
                invoice=inv,
                error="CJ CSRF 토큰을 찾지 못했습니다.",
                source="cj-www",
            )

        detail = await client.post(
            TRACKING_DETAIL,
            data={"_csrf": csrf, "paramInvcNo": inv},
            headers={
                **headers,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": TRACKING_PAGE,
                "Origin": "https://www.cjlogistics.com",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
        )
        detail.raise_for_status()
        payload = detail.json()

    detail_map = payload.get("parcelDetailResultMap") or {}
    summary_map = payload.get("parcelResultMap") or {}
    events_raw = detail_map.get("resultList") or []
    summary_rows = summary_map.get("resultList") or []
    if not events_raw and not summary_rows:
        return CjTrack(
            found=True,
            invoice=inv,
            status="배송준비",
            events=[],
            current_stage_index=0,
            stages=_build_stages(0, []),
            error="운송장이 아직 등록되지 않았거나 배송준비 중입니다.",
            source="cj-www",
        )

    events: list[CjEvent] = []
    for row in events_raw:
        code = str(row.get("crgSt") or "")
        scan = str(row.get("scanNm") or "")
        label = _scan_label(code, scan)
        stage = _map_status(code, label)
        events.append(
            CjEvent(
                stage=stage,
                processed_at=str(row.get("dTime") or ""),
                location=str(row.get("regBranNm") or ""),
                status_code=_norm_code(code) or code,
                raw_status=label,
                note=str(row.get("crgNm") or "").strip(),
            )
        )

    track = _finalize_events(
        str(detail_map.get("paramInvcNo") or inv),
        events,
        source="cj-www",
    )
    return track


async def _fetch_cj_once(inv: str) -> CjTrack:
    # 1순위: 네이버와 같은 next 상세 API
    try:
        track = await _fetch_cj_next_once(inv)
        if track.found and track.events:
            return track
        if track.found and not track.events:
            # 미등록이면 www도 같은 결과일 가능성 높음 → 그대로
            return track
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        track = None

    # 2순위: www 폴백
    return await _fetch_cj_www_once(inv)


async def fetch_cj_tracking(invoice: str) -> CjTrack:
    inv = _normalize_invoice(invoice)
    if not inv:
        return CjTrack(found=False, error="CJ 송장번호가 없습니다.")
    if len(inv) not in {10, 12}:
        return CjTrack(
            found=False,
            invoice=inv,
            error=f"CJ 송장번호 형식이 올바르지 않습니다: {inv}",
        )

    last_error = ""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            track = await _fetch_cj_once(inv)
            if (
                not track.found
                and "CSRF" in (track.error or "")
                and attempt < _MAX_ATTEMPTS
            ):
                last_error = track.error
                await asyncio.sleep(
                    _RETRY_DELAYS_SEC[min(attempt - 1, len(_RETRY_DELAYS_SEC) - 1)]
                )
                continue
            if attempt > 1 and track.found:
                track.error = (track.error or "").strip()
            return track
        except httpx.HTTPError as exc:
            last_error = f"CJ 조회 실패: {_format_http_error(exc)}"
            if attempt < _MAX_ATTEMPTS and _is_transient_error(exc):
                await asyncio.sleep(
                    _RETRY_DELAYS_SEC[min(attempt - 1, len(_RETRY_DELAYS_SEC) - 1)]
                )
                continue
            return CjTrack(found=False, invoice=inv, error=last_error)
        except ValueError as exc:
            last_error = f"CJ 응답 파싱 실패: {exc}"
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(
                    _RETRY_DELAYS_SEC[min(attempt - 1, len(_RETRY_DELAYS_SEC) - 1)]
                )
                continue
            return CjTrack(found=False, invoice=inv, error=last_error)

    return CjTrack(found=False, invoice=inv, error=last_error or "CJ 조회 실패")
