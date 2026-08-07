"""CJ대한통운 공식 배송조회 (웹 endpoint)."""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

TRACKING_PAGE = "https://www.cjlogistics.com/ko/tool/parcel/tracking"
TRACKING_DETAIL = "https://www.cjlogistics.com/ko/tool/parcel/tracking-detail"
_MAX_ATTEMPTS = 4
_RETRY_DELAYS_SEC = (0.8, 1.5, 3.0)

# 표시용 국내배송 단계
STAGE_FLOW = [
    "배송준비",
    "집화",
    "이동중",
    "배송중",
    "배송완료",
]

# CJ crgSt -> 표시 상태
STATUS_BY_CODE = {
    "01": "배송준비",
    "11": "집화",
    "12": "집화",
    "21": "이동중",
    "41": "이동중",
    "42": "이동중",
    "44": "이동중",
    "82": "배송중",
    "84": "배송중",
    "91": "배송완료",
}

STAGE_INDEX = {name: i for i, name in enumerate(STAGE_FLOW)}


@dataclass
class CjEvent:
    stage: str
    processed_at: str = ""
    location: str = ""
    status_code: str = ""
    raw_status: str = ""


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


def _map_status(code: str, scan_nm: str) -> str:
    code = (code or "").strip()
    if code in STATUS_BY_CODE:
        return STATUS_BY_CODE[code]
    text = (scan_nm or "").strip()
    for name in STAGE_FLOW:
        if name in text:
            return name
    aliases = (
        ("배달완료", "배송완료"),
        ("배송완료", "배송완료"),
        ("배송출발", "배송중"),
        ("배달출발", "배송중"),
        ("배송중", "배송중"),
        ("간선", "이동중"),
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
    return text or "배송준비"


def _build_stages(current_index: int, events: list[CjEvent]) -> list[dict[str, Any]]:
    event_by_stage: dict[str, CjEvent] = {}
    for ev in events:
        idx = STAGE_INDEX.get(ev.stage)
        if idx is None:
            continue
        # 같은 단계는 최초 시각 유지
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


async def _fetch_cj_once(inv: str) -> CjTrack:
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
    events_raw = detail_map.get("resultList") or []
    if not events_raw:
        # 미등록/준비중
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
        )

    events: list[CjEvent] = []
    for row in events_raw:
        code = str(row.get("crgSt") or "")
        scan = str(row.get("scanNm") or "")
        stage = _map_status(code, scan)
        events.append(
            CjEvent(
                stage=stage,
                processed_at=str(row.get("dTime") or ""),
                location=str(row.get("regBranNm") or ""),
                status_code=code,
                raw_status=scan,
            )
        )

    latest = events[-1]
    current_idx = max(
        (STAGE_INDEX[ev.stage] for ev in events if ev.stage in STAGE_INDEX),
        default=0,
    )
    status = latest.stage if latest.stage in STAGE_INDEX else _map_status(
        latest.status_code, latest.raw_status
    )
    if status in STAGE_INDEX:
        current_idx = max(current_idx, STAGE_INDEX[status])

    return CjTrack(
        found=True,
        invoice=str(detail_map.get("paramInvcNo") or inv),
        status=status,
        processed_at=latest.processed_at,
        location=latest.location,
        events=list(reversed(events)),  # 최신 먼저
        current_stage_index=current_idx,
        stages=_build_stages(current_idx, events),
    )


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
            # CSRF 미검출도 재시도 (간헐적 HTML 차단/리셋)
            if (
                not track.found
                and "CSRF" in (track.error or "")
                and attempt < _MAX_ATTEMPTS
            ):
                last_error = track.error
                await asyncio.sleep(_RETRY_DELAYS_SEC[min(attempt - 1, len(_RETRY_DELAYS_SEC) - 1)])
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
