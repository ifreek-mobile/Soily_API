import asyncio
import re
from typing import Dict, Tuple

import httpx

# --- GSI が公開する自治体コードマスタ (muni.js) を利用して逆ジオコーディング結果を補完する ---
MUNI_JS_URL = "https://maps.gsi.go.jp/js/muni.js"
MUNI_PATTERN = re.compile(
    r'GSI\.MUNI_ARRAY\["(?P<key>\d+)"\]\s*=\s*\'\d+,(?P<pref>[^,]+),\d+,(?P<city>[^\']+)\''
)

# muni.js の内容は初回アクセス時に読み込み、以後はメモリにキャッシュする
_MUNI_MAP: Dict[str, Tuple[str, str]] | None = None
_MUNI_MAP_LOCK = asyncio.Lock()
# GSI API への同時アクセスを抑えるためのセマフォ（レート制御）
_GSI_SEMAPHORE = asyncio.Semaphore(3)


async def _load_muni_map() -> Dict[str, Tuple[str, str]]:
    """
    GSI が提供する muni.js を取得し、自治体コード → (都道府県名, 市区町村名) の辞書に整形する。
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(MUNI_JS_URL)
        resp.raise_for_status()
        try:
            text = resp.content.decode("utf-8")
        except UnicodeDecodeError:
            text = resp.content.decode("shift_jis")
    mapping: Dict[str, Tuple[str, str]] = {}
    for match in MUNI_PATTERN.finditer(text):
        mapping[match.group("key")] = (
            match.group("pref"),
            match.group("city"),
        )
    return mapping


async def _ensure_muni_map() -> Dict[str, Tuple[str, str]]:
    """
    muni.js の辞書を遅延ロードし、以後は再利用する。
    非同期環境でも二重読み込みを防ぐため Lock を使用。
    """
    global _MUNI_MAP
    if _MUNI_MAP is None:
        async with _MUNI_MAP_LOCK:
            if _MUNI_MAP is None:
                _MUNI_MAP = await _load_muni_map()
    return _MUNI_MAP


async def resolve_pref_city(
    latitude: str | None,
    longitude: str | None,
) -> tuple[str | None, str | None]:
    """
    緯度・経度から GSI 逆ジオコーダを呼び出し、muni.js を使って都道府県・市区町村名を返す。
    連続アクセス制限やタイムアウトを考慮し、必要に応じてリトライを行う。
    """
    if not latitude or not longitude:
        return None, None
    try:
        lat = float(latitude)
        lon = float(longitude)
    except ValueError:
        return None, None

    async with _GSI_SEMAPHORE:
        muni_map = await _ensure_muni_map()
        params = {"lat": lat, "lon": lon}
        for attempt in (0, 1):
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        "https://mreversegeocoder.gsi.go.jp/reverse-geocoder/LonLatToAddress",
                        params=params,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                break
            except Exception:
                # 1回目の失敗は待機してリトライ、それでも失敗したら None を返す
                if attempt == 0:
                    await asyncio.sleep(0.5)
                else:
                    return None, None

    muni_cd = data.get("results", {}).get("muniCd")
    if not muni_cd:
        return None, None

    # muni.js のキーは先頭ゼロが無い形式なので str(int(...)) で正規化する
    key = str(int(muni_cd))
    return muni_map.get(key, (None, None))
