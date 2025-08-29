# 1. ライブラリのインストール
# ターミナルで以下を実行
# pip install inference-sdk

# 2. Pythonコード例
from inference_sdk import InferenceHTTPClient

# クライアントの初期化
CLIENT = InferenceHTTPClient(
    api_url="https://serverless.roboflow.com",
    api_key="zxCjDibvvTfkYK30nT45"  # 公開例キー (本番は環境変数推奨)
)

# 画像の推論
# 'your_image.jpg'の部分を判定したい画像パスに変更してください
result = CLIENT.infer(
    "images/kokusei.jpg",
    model_id="detecting-diseases/5"
)

# predictions はリスト
preds = result.get("predictions", [])

if not preds:
    print("検出なし")
else:
    # 全クラス表示（最初だけなら preds[0]['class']）
    for p in preds:
        print(f"{p.get('class')} confidence={p.get('confidence', 0):.3f}")
