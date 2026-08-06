"""통관 상태 변경 시 카카오톡 나에게 보내기.

환경변수:
  UNIPASS_API_KEY
  TRACK_HBL
  TRACK_YEAR (optional)
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
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def fingerprint(cargo: dict) -> str:
    latest = ""
    events = cargo.get("events") or []
    if events:
        latest = f"{events[0].get('stage','')}|{events[0].get('processed_at','')}"
    return "|".join(
        [
            cargo.get("status") or "",
            cargo.get("processed_at") or "",
            cargo.get("cargo_no") or "",
            latest,
        ]
    )


PUBLIC_PAGE_URL = os.getenv(
    "PUBLIC_PAGE_URL",
    "https://ybs8625-cmd.github.io/customs-tracker/",
).strip()


def format_processed_at(raw: str) -> str:
    """Unipass processed_at (YYYYMMDDHHmmss) -> yy-MM-dd HH:mm:ss."""
    text = (raw or "").strip()
    if len(text) >= 14 and text[:14].isdigit():
        return (
            f"{text[2:4]}-{text[4:6]}-{text[6:8]} "
            f"{text[8:10]}:{text[10:12]}:{text[12:14]}"
        )
    return text or "-"


def build_message(cargo: dict, prev_status: str) -> str:
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


async def main() -> int:
    hbl = os.getenv("TRACK_HBL", "").strip()
    if not hbl:
        print("TRACK_HBL 이 없습니다.", file=sys.stderr)
        return 2

    year_raw = os.getenv("TRACK_YEAR", "").strip()
    year = int(year_raw) if year_raw else None
    dry_run = os.getenv("DRY_RUN", "").strip() in {"1", "true", "TRUE", "yes"}

    cargo_obj = await fetch_cargo(hbl=hbl, year=year)
    cargo = cargo_obj.to_dict()
    if not cargo_obj.found:
        print(f"조회 실패: {cargo_obj.error}", file=sys.stderr)
        return 1

    path = _state_path()
    state = load_state(path)
    key = f"{hbl}:{cargo.get('year')}"
    prev = state.get(key) or {}
    prev_fp = prev.get("fingerprint", "")
    curr_fp = fingerprint(cargo)
    changed = prev_fp != curr_fp

    print(
        json.dumps(
            {
                "hbl": hbl,
                "status": cargo.get("status"),
                "changed": changed,
                "source": cargo.get("source"),
                "fingerprint": curr_fp,
            },
            ensure_ascii=False,
        )
    )

    if changed:
        msg = build_message(cargo, prev.get("status", ""))
        if dry_run:
            print("DRY_RUN: 전송 생략")
            print(msg)
        else:
            # 첫 실행(이전 상태 없음)도 알림 — 원하면 FIRST_NOTIFY=0 으로 끌 수 있음
            first = not prev_fp
            notify_first = os.getenv("FIRST_NOTIFY", "1").strip() not in {"0", "false", "FALSE"}
            if first and not notify_first:
                print("첫 스냅샷만 저장 (알림 생략)")
            else:
                try:
                    await send_to_me(msg)
                    print("카톡 전송 완료")
                except KakaoError as exc:
                    print(f"카톡 전송 실패: {exc}", file=sys.stderr)
                    return 3

        state[key] = {
            "hbl": hbl,
            "year": cargo.get("year"),
            "status": cargo.get("status"),
            "product_name": cargo.get("product_name") or "",
            "processed_at": cargo.get("processed_at"),
            "fingerprint": curr_fp,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        save_state(path, state)
    else:
        print("변경 없음")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
