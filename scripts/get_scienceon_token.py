import json
from datetime import datetime
from urllib.parse import quote

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad


CLIENT_ID = "53b79f24abae0b2f7c8ee1a44546f86495050f0c0ab3a7393418a9e43b4008fb"
API_KEY = "65928fb19c20415baa48ba9b5db7df4e"
MAC_ADDRESS = "00-A5-54-91-23-37"

TOKEN_REQUEST_URL = "https://apigateway.kisti.re.kr/tokenrequest.do"


def build_accounts_payload(mac_address: str) -> str:
    """
    문서 기준:
    {
        mac_address: 등록된 맥주소,
        datetime: YYYYMMDDHHMMSS
    }
    """
    payload = {
        "mac_address": mac_address,
        "datetime": datetime.now().strftime("%Y%m%d%H%M%S"),
    }
    # 공백 없이 직렬화
    return json.dumps(payload, separators=(",", ":"))


def encrypt_accounts_ecb(payload: str, api_key: str) -> str:
    """
    1차 시도:
    AES-256-ECB + PKCS7 padding
    - API Key 32자리 문자열을 그대로 UTF-8 바이트 키로 사용
    - 결과를 URI Encoding
    """
    key = api_key.encode("utf-8")  # 32 bytes 기대
    cipher = AES.new(key, AES.MODE_ECB)
    encrypted = cipher.encrypt(pad(payload.encode("utf-8"), AES.block_size))
    return quote(encrypted.hex())  # hex 인코딩 + URL 인코딩


async def request_token(client_id: str, accounts: str) -> dict:
    params = {
        "accounts": accounts,
        "client_id": client_id,
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(TOKEN_REQUEST_URL, params=params)
        response.raise_for_status()

        # ScienceON 안내문 기준으로 JSON 응답 기대
        # 실패 시 text로 먼저 확인
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()

        return {
            "raw_text": response.text,
            "content_type": content_type,
        }


async def main():
    payload = build_accounts_payload(MAC_ADDRESS)
    print("[1] payload =", payload)

    accounts = encrypt_accounts_ecb(payload, API_KEY)
    print("[2] encoded accounts =", accounts[:120] + "...")

    result = await request_token(CLIENT_ID, accounts)
    print("[3] token response =")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())