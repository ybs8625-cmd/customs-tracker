"""카카오톡 '나에게 보내기' 알림."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlencode

import httpx

TOKEN_URL = "https://kauth.kakao.com/oauth/token"
MEMO_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"


class KakaoError(RuntimeError):
    pass


def _rest_key() -> str:
    key = os.getenv("KAKAO_REST_API_KEY", "").strip()
    if not key:
        raise KakaoError("KAKAO_REST_API_KEY 가 없습니다.")
    return key


def _client_secret() -> str | None:
    secret = os.getenv("KAKAO_CLIENT_SECRET", "").strip()
    return secret or None


def _with_client_secret(data: dict[str, str]) -> dict[str, str]:
    secret = _client_secret()
    if secret:
        data = {**data, "client_secret": secret}
    return data


async def refresh_access_token(refresh_token: str | None = None) -> dict[str, str]:
    refresh_token = (refresh_token or os.getenv("KAKAO_REFRESH_TOKEN", "")).strip()
    if not refresh_token:
        raise KakaoError("KAKAO_REFRESH_TOKEN 가 없습니다.")

    data = _with_client_secret(
        {
            "grant_type": "refresh_token",
            "client_id": _rest_key(),
            "refresh_token": refresh_token,
        }
    )
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(TOKEN_URL, data=data)
        payload = resp.json()
        if resp.status_code >= 400:
            raise KakaoError(f"토큰 갱신 실패: {payload}")
        return {
            "access_token": payload["access_token"],
            "refresh_token": payload.get("refresh_token", refresh_token),
        }


async def send_to_me(text: str, access_token: str | None = None) -> dict[str, Any]:
    """카카오톡 나에게 보내기 (text 템플릿)."""
    token = (access_token or os.getenv("KAKAO_ACCESS_TOKEN", "")).strip()
    if not token:
        refreshed = await refresh_access_token()
        token = refreshed["access_token"]

    # 카카오 기본 템플릿 제한: text 최대 200자
    body = text.strip()
    if len(body) > 200:
        body = body[:197] + "..."

    template = {
        "object_type": "text",
        "text": body,
        "link": {
            "web_url": "https://unipass.customs.go.kr",
            "mobile_web_url": "https://unipass.customs.go.kr",
        },
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
    }
    form = {"template_object": json.dumps(template, ensure_ascii=False)}

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(MEMO_URL, headers=headers, data=form)
        payload = resp.json()
        if resp.status_code >= 400:
            # access token 만료면 refresh 후 1회 재시도
            if resp.status_code == 401 or str(payload.get("code")) in {"-401", "401"}:
                refreshed = await refresh_access_token()
                headers["Authorization"] = f"Bearer {refreshed['access_token']}"
                resp = await client.post(MEMO_URL, headers=headers, data=form)
                payload = resp.json()
                if resp.status_code >= 400:
                    raise KakaoError(f"카톡 전송 실패: {payload}")
                return payload
            raise KakaoError(f"카톡 전송 실패: {payload}")
        return payload


def auth_url(redirect_uri: str, state: str = "customs") -> str:
    params = {
        "client_id": _rest_key(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "talk_message",
        "state": state,
    }
    return f"https://kauth.kakao.com/oauth/authorize?{urlencode(params)}"


async def exchange_code(code: str, redirect_uri: str) -> dict[str, Any]:
    data = _with_client_secret(
        {
            "grant_type": "authorization_code",
            "client_id": _rest_key(),
            "redirect_uri": redirect_uri,
            "code": code,
        }
    )
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(TOKEN_URL, data=data)
        payload = resp.json()
        if resp.status_code >= 400:
            raise KakaoError(f"인가코드 교환 실패: {payload}")
        return payload
