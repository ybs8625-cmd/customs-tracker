"""통관·배송 ETA 추정 (경험적 휴리스틱)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any


@dataclass
class EtaEstimate:
    clearance_days_min: int
    clearance_days_max: int
    clearance_eta_from: str
    clearance_eta_to: str
    cj_pickup: str
    receive_eta_from: str
    receive_eta_to: str
    note: str
    mode: str  # sea | air | unknown

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_date(value: str) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(value[: len(datetime.now().strftime(fmt))], fmt)
        except ValueError:
            continue
    # 2026-08-05 12:37:21
    try:
        return datetime.fromisoformat(value.replace(".", "-"))
    except ValueError:
        return None


def _is_sea(carrier: str, vessel: str, cargo_type: str, forwarder: str) -> bool | None:
    # 선박명이 있으면 해운
    if vessel and vessel.strip():
        return True
    blob = " ".join([carrier, cargo_type, forwarder])
    # '해운항공' 상호는 항공으로 오인하지 않음
    normalized = blob.replace("해운항공", "FORWARDER")
    if any(k in normalized for k in ("항공", "AIR", "flight", "항공기", "항공편")):
        return False
    if any(k in blob for k in ("해운", "선박", "vessel", "ship", "SEA")):
        return True
    return None


def estimate_eta(
    *,
    arrival_date: str,
    status: str,
    carrier: str = "",
    vessel: str = "",
    cargo_type: str = "",
    forwarder: str = "",
    current_stage_index: int = -1,
) -> EtaEstimate:
    arrival = _parse_date(arrival_date) or datetime.now()
    sea = _is_sea(carrier, vessel, cargo_type, forwarder)

    # 윈핸드·인천항 해운 최근 패턴 기준 / 항공은 더 짧음
    if sea is False:
        mode = "air"
        dmin, dmax = 1, 2
        note = "항공 특송 평균(약 1~2일) 기준 추정"
    elif sea is True:
        mode = "sea"
        dmin, dmax = 3, 4
        note = "해운·인천항 특송 최근 패턴(약 3~4일) 기준 추정. 해운은 주말·공휴일 휴무인 경우가 많음"
    else:
        mode = "unknown"
        dmin, dmax = 2, 4
        note = "운송수단 불명 — 일반 특송 범위로 추정"

    # 이미 후반 단계면 남은 일수 축소
    if current_stage_index >= 7 or "물품반출" in status:
        dmin, dmax = 0, 0
        note = "물품반출 완료 — CJ 집화·배송 단계로 보면 됩니다"
    elif current_stage_index >= 6 or "수입신고수리" in status:
        dmin, dmax = 0, 1
        note = "수입신고수리 이후 — 반출·집화가 임박한 상태"
    elif current_stage_index >= 5 or ("수입신고" in status and "수리" not in status):
        dmin, dmax = 0, 2

    clear_from = arrival + timedelta(days=dmin)
    clear_to = arrival + timedelta(days=dmax)

    # 주말이면 해운은 월요일로 보정
    def bump_weekend(dt: datetime) -> datetime:
        if mode == "sea" and dt.weekday() >= 5:  # Sat/Sun
            return dt + timedelta(days=(7 - dt.weekday()))
        return dt

    clear_from = bump_weekend(clear_from)
    clear_to = bump_weekend(clear_to)

    # CJ: 통관 당일 집화 + 익일 배송 가정
    receive_from = clear_from + timedelta(days=1)
    receive_to = clear_to + timedelta(days=1)

    return EtaEstimate(
        clearance_days_min=dmin,
        clearance_days_max=dmax,
        clearance_eta_from=clear_from.strftime("%Y-%m-%d"),
        clearance_eta_to=clear_to.strftime("%Y-%m-%d"),
        cj_pickup="통관완료 당일 집화 가능성 높음",
        receive_eta_from=receive_from.strftime("%Y-%m-%d"),
        receive_eta_to=receive_to.strftime("%Y-%m-%d"),
        note=note,
        mode=mode,
    )
