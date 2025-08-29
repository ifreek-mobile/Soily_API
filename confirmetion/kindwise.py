import requests
import base64
import os

# plant.id APIのエンドポイント
url = "https://api.plant.id/v3/health_assessment"

# あなたのAPIキー（X-Api-Key）
API_KEY = os.getenv(
    "PLANT_ID_API_KEY",
    "49UYiWTKuJPqmrt3RE3sYvORkz3tb8kAkPC1qT8Y2N32CnYMJx"
)

# 画像ファイルをBase64に変換


def encode_image(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


# リクエストボディ作成
payload = {
    "images": [
        encode_image("images/udonko.jpg")
    ],
    # "health": "only"  # 必要なら指定 (デフォルトで health 情報含まれる)
}

# リクエストヘッダー
headers = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Api-Key": API_KEY
}

# POSTリクエスト送信
resp = requests.post(url, json=payload, headers=headers)

# 結果を表示 (元の情報)
print(f"Status: {resp.status_code}")
print(f"Headers: {resp.headers}")

try:
    json_data = resp.json()
    print("RAW JSON 取得済み (省略表示): access_token =", json_data.get("access_token"))
except ValueError:
    print("Non-JSON body head:", resp.text[:500])
    raise SystemExit(1)

# ---------------- ここから必要情報のみ抽出して表示 ----------------
result = json_data.get("result") or {}
is_plant = result.get("is_plant") or {}
is_healthy = result.get("is_healthy") or {}
disease_block = result.get("disease") or {}
suggestions = disease_block.get("suggestions") or []

# 安全に値取得（無い場合は None）
is_plant_prob = is_plant.get("probability")
is_plant_threshold = is_plant.get("threshold")
is_healthy_prob = is_healthy.get("probability")
is_healthy_threshold = is_healthy.get("threshold")

print("\n■ 判定結果（Summary）")
if is_plant_prob is not None and is_plant_threshold is not None:
    print(f"植物確率: {is_plant_prob:.2%}（閾値: {is_plant_threshold}）")
else:
    print("植物判定情報が不足しています。")
if is_healthy_prob is not None and is_healthy_threshold is not None:
    print(f"健康確率: {is_healthy_prob:.2%}（閾値: {is_healthy_threshold}）")
else:
    print("健康判定情報が不足しています。")

print("\n■ 病害候補の解説")
if not suggestions:
    print("病害候補は返却されませんでした。")
else:
    for s in suggestions:
        name = s.get("name", "Unknown")
        prob = s.get("probability")
        if prob is not None:
            print(f"{name}: {prob:.2%}")
        else:
            print(f"{name}: (確率情報なし)")

print("\n■ まとめ・現場への示唆")
if (is_healthy_prob is None) or (is_healthy_threshold is None):
    print("健康判定が不完全なため追加データ取得/再実行を推奨。")
else:
    if is_healthy_prob < is_healthy_threshold:
        print("健康とは判定されず、病害の可能性が高いです。上位候補を重点的に観察・早期対策してください。")
        if suggestions:
            top = suggestions[0]
            print(f"最有力候補: {top.get('name')} (約{top.get('probability'):.1%})")
    else:
        print("健康と判断されました。現状大きな問題はなさそうです。定期観察のみで可。")

# 必要なら：抽出した要約だけを JSON 形式で利用したい場合
# summary_dict = {
#     "is_plant_probability": is_plant_prob,
#     "is_plant_threshold": is_plant_threshold,
#     "is_healthy_probability": is_healthy_prob,
#     "is_healthy_threshold": is_healthy_threshold,
#     "disease_candidates": [
#         {"name": s.get("name"), "probability": s.get("probability")}
#         for s in suggestions
#     ]
# }
# print(json.dumps(summary_dict, ensure_ascii=False, indent=2))
