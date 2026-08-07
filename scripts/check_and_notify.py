"""통관/국내배송 상태 변경 시 카카오톡 나에게 보내기.

환경변수:
  UNIPASS_API_KEY
  TRACK_HBL
  TRACK_YEAR (optional)
  TRACK_CJ_INVOICE (optional, default TRACK_HBL)
  KAKAO_REST_API_KEY
  KAKAO_REFRESH_TOKEN
  STATE_PATH (optional, default data/state.json)
  DRY_RUN=1 이면 전송 생략
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from app.cj import fetch_cj_tracking  # noqa: E402
from app.kakao import KakaoError, send_to_me  # noqa: E402
from app.unipass import fetch_cargo  # noqa: E402


def _state_path() -> Path:
    raw = os.getenv("STATE_PATH", "data/state.json")
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return path


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2)
    path.write_text(payload, encoding="utf-8")
    pages_state = ROOT / "docs" / "state.json"
    pages_state.parent.mkdir(parents=True, exist_ok=True)
    pages_state.write_text(payload + "\n", encoding="utf-8")


PUBLIC_PAGE_URL = os.getenv(
    "PUBLIC_PAGE_URL",
    "https://ybs8625-cmd.github.io/customs-tracker/",
).strip()


def format_processed_at(raw: str) -> str:
    """Unipass/CJ datetime -> yyyy-MM-dd HH:mm:ss."""
    text = (raw or "").strip()
    if len(text) >= 14 and text[:14].isdigit():
        return (
            f"{text[0:4]}-{text[4:6]}-{text[6:8]} "
            f"{text[8:10]}:{text[10:12]}:{text[12:14]}"
        )
    if len(text) >= 19 and text[4] == "-" and text[10] == " ":
        return text[:19]
    return text or "-"


def customs_fingerprint(cargo: dict) -> str:
    events = cargo.get("events") or []
    # 최신 이벤트 3개까지 포함 — 헤더 상태가 그대로여도 상세 갱신을 감지
    latest_bits = [
        f"{ev.get('stage', '')}|{ev.get('processed_at', '')}" for ev in events[:3]
    ]
    return "|".join(
        [
            cargo.get("status") or "",
            cargo.get("processed_at") or "",
            cargo.get("cargo_no") or "",
            *latest_bits,
        ]
    )


def _domestic_event_soft_key(ev: dict) -> tuple[str, str, str]:
    """같은 스캔을 location 유무와 무관하게 묶기 위한 키."""
    return (
        str(ev.get("status_code") or "").upper(),
        str(ev.get("raw_status") or ev.get("stage") or ""),
        str(ev.get("processed_at") or ""),
    )


def merge_domestic_events(
    prev_events: list | None,
    curr_events: list | None,
) -> list[dict]:
    """CJ가 예전 스캔을 빼도 이전 state 이력을 누적 보존 (최신 먼저)."""
    from app.cj import STAGE_INDEX

    merged: dict[tuple[str, str, str], dict] = {}
    for ev in list(prev_events or []) + list(curr_events or []):
        if not isinstance(ev, dict):
            continue
        key = _domestic_event_soft_key(ev)
        if not any(key):
            continue
        old = merged.get(key)
        if old is None:
            merged[key] = {
                "stage": ev.get("stage") or "",
                "processed_at": ev.get("processed_at") or "",
                "location": ev.get("location") or "",
                "status_code": ev.get("status_code") or "",
                "raw_status": ev.get("raw_status") or ev.get("stage") or "",
                "note": ev.get("note") or "",
            }
            continue
        # 더 풍부한 필드 우선
        if not old.get("location") and ev.get("location"):
            old["location"] = ev.get("location") or ""
        if not old.get("note") and ev.get("note"):
            old["note"] = ev.get("note") or ""
        if not old.get("raw_status") and ev.get("raw_status"):
            old["raw_status"] = ev.get("raw_status") or ""
        if not old.get("stage") and ev.get("stage"):
            old["stage"] = ev.get("stage") or ""
        merged[key] = old

    events = list(merged.values())
    events.sort(
        key=lambda ev: (
            str(ev.get("processed_at") or ""),
            STAGE_INDEX.get(str(ev.get("stage") or ""), -1),
        )
    )
    return list(reversed(events))


def domestic_fingerprint(track: dict) -> str:
    """모든 스캔 이벤트를 포함해 중간 이동(동일 단계)도 변경으로 감지."""
    events = track.get("events") or []
    event_bits = [
        "|".join(
            [
                str(ev.get("status_code") or ""),
                str(ev.get("raw_status") or ev.get("stage") or ""),
                str(ev.get("processed_at") or ""),
                str(ev.get("location") or ""),
            ]
        )
        for ev in events
    ]
    return "|".join(
        [
            track.get("invoice") or "",
            track.get("status") or "",
            track.get("processed_at") or "",
            str(len(events)),
            *event_bits,
        ]
    )


def _compact_when(raw: str) -> str:
    """yyyy-MM-dd HH:mm:ss -> MM-dd HH:mm (카톡 길이 절약)."""
    text = format_processed_at(raw)
    if len(text) >= 16 and text[4] == "-" and text[10] == " ":
        return f"{text[5:10]} {text[11:16]}"
    return text


def _event_line(ev: dict) -> str:
    when = _compact_when(str(ev.get("processed_at") or ""))
    label = (ev.get("raw_status") or ev.get("stage") or "-").strip()
    loc = (ev.get("location") or "").strip()
    # "[집화]코어물류" -> "코어물류"
    loc = loc.replace("[집화]", "").replace("[", "").replace("]", "").strip()
    parts = [p for p in (when, label, loc) if p]
    return " · ".join(parts)


def build_customs_message(cargo: dict, prev_status: str) -> str:
    hbl = cargo.get("hbl") or os.getenv("TRACK_HBL", "")
    status = cargo.get("status") or "-"
    product = (cargo.get("product_name") or "").strip()
    when = format_processed_at(cargo.get("processed_at") or "")
    lines = [
        "[통관 업데이트 알림]",
        f"송장번호 - {hbl}",
        f"{prev_status or '-'} -> {status}",
        f"변경일자 {when}",
    ]
    if product:
        lines.append(product)
    if PUBLIC_PAGE_URL:
        lines.append(f"바로조회 {PUBLIC_PAGE_URL}")
    return "\n".join(lines)


def build_domestic_message(
    track: dict,
    prev_status: str,
    *,
    prev_events: list | None = None,
) -> str:
    """최신 변화 + 진행 이력(시간)을 카톡 200자 안에 최대한 담음."""
    invoice = track.get("invoice") or ""
    status = track.get("status") or "-"
    events = list(track.get("events") or [])
    prev_events = list(prev_events or [])
    prev_keys = {
        (
            str(ev.get("status_code") or ""),
            str(ev.get("raw_status") or ev.get("stage") or ""),
            str(ev.get("processed_at") or ""),
            str(ev.get("location") or ""),
        )
        for ev in prev_events
    }
    new_events = [
        ev
        for ev in events
        if (
            str(ev.get("status_code") or ""),
            str(ev.get("raw_status") or ev.get("stage") or ""),
            str(ev.get("processed_at") or ""),
            str(ev.get("location") or ""),
        )
        not in prev_keys
    ]

    header = [
        "[국내배송 업데이트]",
        f"송장 {invoice}",
        f"{prev_status or '-'} → {status}",
    ]
    footer = [f"바로조회 {PUBLIC_PAGE_URL}"] if PUBLIC_PAGE_URL else []

    if new_events:
        # 신규 스캔을 위에, 이어서 최근 이력
        older = [ev for ev in events if ev not in new_events]
        history_source = list(new_events) + older
    else:
        history_source = events
    history_lines: list[str] = []
    for ev in history_source[:8]:
        line = _event_line(ev)
        if line:
            prefix = "+" if new_events and ev in new_events else "·"
            history_lines.append(f"{prefix} {line}")

    # 200자 제한에 맞춰 이력부터 줄임
    def pack(hist: list[str]) -> str:
        return "\n".join(header + hist + footer)

    while history_lines and len(pack(history_lines)) > 200:
        history_lines.pop()
    body = pack(history_lines)
    if len(body) > 200:
        body = body[:197] + "..."
    return body


def _legacy_customs(prev: dict) -> dict:
    """이전 flat state -> customs 섹션."""
    if prev.get("customs"):
        return prev["customs"]
    if not prev:
        return {}
    return {
        "status": prev.get("status", ""),
        "fingerprint": prev.get("fingerprint", ""),
        "processed_at": prev.get("processed_at", ""),
        "product_name": prev.get("product_name", ""),
        "current_stage_index": prev.get("current_stage_index", -1),
        "stages": prev.get("stages") or [],
    }


def _is_transient_domestic_error(error: str) -> bool:
    text = (error or "").strip()
    return text.startswith(
        (
            "CJ 조회 실패:",
            "CJ 응답 파싱 실패:",
            "CJ CSRF 토큰을 찾지 못했습니다.",
        )
    )


def _is_meaningful_domestic(track: dict) -> bool:
    """미등록/배송준비(이벤트 없음)는 알림 노이즈로 보고 제외."""
    if not track.get("found"):
        return False
    status = (track.get("status") or "").strip()
    events = track.get("events") or []
    if not status:
        return False
    if status == "배송준비" and not events:
        return False
    return True


async def _send_messages(parts: list[str], *, dry_run: bool) -> None:
    if not parts:
        return
    combined = "\n\n".join(parts)
    payloads = [combined] if len(combined) <= 200 else parts
    for msg in payloads:
        if dry_run:
            print("DRY_RUN: 전송 생략")
            print(msg)
            continue
        await send_to_me(msg)
        print("카톡 전송 완료")


async def main() -> int:
    hbl = os.getenv("TRACK_HBL", "").strip()
    if not hbl:
        print("TRACK_HBL 이 없습니다.", file=sys.stderr)
        return 2

    year_raw = os.getenv("TRACK_YEAR", "").strip()
    year = int(year_raw) if year_raw else None
    cj_invoice = (os.getenv("TRACK_CJ_INVOICE") or hbl).strip()
    dry_run = os.getenv("DRY_RUN", "").strip() in {"1", "true", "TRUE", "yes"}
    notify_first = os.getenv("FIRST_NOTIFY", "1").strip() not in {"0", "false", "FALSE"}
    force_notify = os.getenv("FORCE_NOTIFY", "").strip() in {"1", "true", "TRUE", "yes"}

    cargo_obj = await fetch_cargo(hbl=hbl, year=year)
    cargo = cargo_obj.to_dict()
    customs_ok = bool(cargo_obj.found)
    if not customs_ok:
        print(f"통관 조회 실패: {cargo_obj.error}", file=sys.stderr)

    cj_obj = await fetch_cj_tracking(cj_invoice)
    domestic = cj_obj.to_dict()

    path = _state_path()
    state = load_state(path)
    year_key = cargo.get("year") or year or datetime.now(timezone.utc).year
    key = f"{hbl}:{year_key}"
    prev = state.get(key) or {}
    # 연도 키 불일치 시 기존 HBL 엔트리 fallback
    if not prev:
        for cand_key, cand in state.items():
            if str(cand_key).startswith(f"{hbl}:"):
                prev = cand
                key = str(cand_key)
                break
    prev_customs = _legacy_customs(prev)
    prev_domestic = prev.get("domestic") or {}

    domestic_stale = False
    if (
        not domestic.get("found")
        and _is_transient_domestic_error(str(domestic.get("error") or ""))
        and prev_domestic.get("found")
    ):
        # 일시 장애로 성공 스냅샷을 덮어쓰지 않음 (변경 감지/알림 누락 방지)
        domestic_stale = True
        print(
            f"CJ 일시 조회 실패 — 이전 국내배송 상태 유지: {domestic.get('error')}",
            file=sys.stderr,
        )
        domestic = {
            **prev_domestic,
            "error": domestic.get("error") or prev_domestic.get("error") or "",
        }
    elif domestic.get("found"):
        # CJ 응답에서 빠진 과거 스캔(행낭포장 등)도 누적 보존
        merged_events = merge_domestic_events(
            prev_domestic.get("events") or [],
            domestic.get("events") or [],
        )
        if len(merged_events) != len(domestic.get("events") or []):
            print(
                f"국내배송 이력 병합: cj={len(domestic.get('events') or [])} "
                f"-> merged={len(merged_events)}"
            )
        domestic = {**domestic, "events": merged_events}

    curr_customs_fp = customs_fingerprint(cargo) if customs_ok else ""
    curr_domestic_fp = domestic_fingerprint(domestic)
    prev_customs_fp = prev_customs.get("fingerprint", "")
    prev_domestic_fp = prev_domestic.get("fingerprint", "")

    customs_changed = bool(customs_ok and prev_customs_fp != curr_customs_fp)
    domestic_changed = (not domestic_stale) and prev_domestic_fp != curr_domestic_fp
    prev_clearance_done = bool(prev.get("clearance_done"))
    if customs_ok:
        status_text = cargo.get("status") or ""
        clearance_done = int(cargo.get("current_stage_index", -1)) >= 7 or any(
            key in status_text for key in ("물품반출", "반출신고", "반출완료")
        )
    else:
        clearance_done = prev_clearance_done

    # 이미 통관 완료·국내배송 단계면 이후 통관 상세 변동은 알림하지 않음
    # (완료 직전 마지막 통관 갱신 1회는 prev_clearance_done=False 라서 알림됨)
    suppress_customs_notify = prev_clearance_done or (
        clearance_done and _is_meaningful_domestic(prev_domestic)
    )

    print(
        json.dumps(
            {
                "hbl": hbl,
                "customs_ok": customs_ok,
                "customs_status": cargo.get("status") if customs_ok else "",
                "customs_changed": customs_changed,
                "customs_error": "" if customs_ok else (cargo_obj.error or ""),
                "cj_invoice": cj_invoice,
                "domestic_status": domestic.get("status"),
                "domestic_found": domestic.get("found"),
                "domestic_changed": domestic_changed,
                "domestic_stale": domestic_stale,
                "domestic_error": domestic.get("error") or "",
                "clearance_done": clearance_done,
                "suppress_customs_notify": suppress_customs_notify,
                "force_notify": force_notify,
            },
            ensure_ascii=False,
        )
    )

    parts: list[str] = []
    if force_notify:
        print("FORCE_NOTIFY=1 — 현재 상태 기준으로 알림 전송")
        if customs_ok and not suppress_customs_notify:
            parts.append(build_customs_message(cargo, prev_customs.get("status", "")))
        elif customs_ok and suppress_customs_notify:
            print("통관 완료·국내배송 단계 — 통관 카톡 알림 생략")
        if domestic.get("found") and _is_meaningful_domestic(domestic):
            parts.append(
                build_domestic_message(
                    domestic,
                    prev_domestic.get("status", ""),
                    prev_events=prev_domestic.get("events") or [],
                )
            )
    else:
        if customs_changed:
            first = not prev_customs_fp
            if suppress_customs_notify:
                print("통관 완료·국내배송 단계 — 통관 카톡 알림 생략")
            elif first and not notify_first:
                print("통관 첫 스냅샷만 저장 (알림 생략)")
            else:
                parts.append(
                    build_customs_message(cargo, prev_customs.get("status", ""))
                )

        if domestic_changed and domestic.get("found"):
            if not _is_meaningful_domestic(domestic):
                # 미등록/배송준비 스냅샷은 저장만 하고 알림하지 않음
                print("국내배송 미등록/배송준비 — 카톡 알림 생략")
            else:
                # 집화 이후 스캔 이력 변화(동일 단계 이동 포함)마다 알림
                parts.append(
                    build_domestic_message(
                        domestic,
                        prev_domestic.get("status", ""),
                        prev_events=prev_domestic.get("events") or [],
                    )
                )

    if parts:
        try:
            await _send_messages(parts, dry_run=dry_run)
        except KakaoError as exc:
            print(f"카톡 전송 실패: {exc}", file=sys.stderr)
            return 3
    elif force_notify:
        print("FORCE_NOTIFY 이지만 보낼 알림이 없습니다.")
    elif not customs_changed and not domestic_changed:
        print("변경 없음")

    now = datetime.now(timezone.utc).isoformat()
    customs_section = (
        {
            "status": cargo.get("status"),
            "product_name": cargo.get("product_name") or "",
            "processed_at": cargo.get("processed_at"),
            "fingerprint": curr_customs_fp,
            "current_stage_index": cargo.get("current_stage_index", -1),
            "stages": cargo.get("stages") or [],
        }
        if customs_ok
        else {
            "status": prev_customs.get("status", ""),
            "product_name": prev_customs.get("product_name", ""),
            "processed_at": prev_customs.get("processed_at", ""),
            "fingerprint": prev_customs_fp,
            "current_stage_index": prev_customs.get("current_stage_index", -1),
            "stages": prev_customs.get("stages") or [],
            "error": cargo_obj.error or "",
        }
    )
    domestic_section = {
        "invoice": domestic.get("invoice") or cj_invoice,
        "status": domestic.get("status") or "",
        "processed_at": domestic.get("processed_at") or "",
        "location": domestic.get("location") or "",
        "fingerprint": curr_domestic_fp if not domestic_stale else prev_domestic_fp,
        "found": bool(domestic.get("found")),
        "error": domestic.get("error") or "",
        "current_stage_index": domestic.get("current_stage_index", -1),
        "stages": domestic.get("stages") or [],
        # Pages/카톡용 상세 스캔 이력 (최신 먼저)
        "events": domestic.get("events") or [],
    }

    state[key] = {
        "hbl": hbl,
        "year": year_key if customs_ok else prev.get("year", year_key),
        "clearance_done": clearance_done,
        "customs": customs_section,
        "domestic": domestic_section,
        # Pages 하위호환: 통관 필드를 루트에도 유지
        "status": customs_section.get("status"),
        "product_name": customs_section.get("product_name") or "",
        "processed_at": customs_section.get("processed_at"),
        "fingerprint": customs_section.get("fingerprint"),
        "current_stage_index": customs_section.get("current_stage_index", -1),
        "stages": customs_section.get("stages") or [],
        "updated_at": (
            now
            if customs_changed or domestic_changed
            else prev.get("updated_at") or now
        ),
    }
    save_state(path, state)

    # 상태 커밋 단계가 실행되도록, 한쪽이라도 처리됐으면 성공 종료.
    # 양쪽 모두 실패일 때만 non-zero.
    if not customs_ok and not domestic.get("found") and not domestic_stale:
        return 1
    if not customs_ok:
        print("통관 조회는 실패했지만 국내배송 처리는 완료했습니다.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
