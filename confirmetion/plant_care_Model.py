# plant_care_Model.py
# 依存: pip install inference-sdk
# 用途: plant_care/9 モデルで植物画像を推論しクラスと信頼度を表示
# 使い方:
#   python confirmetion/plant_care_Model.py               # デフォルト画像
#   python confirmetion/plant_care_Model.py images/foo.jpg # 任意画像
#   export ROBOFLOW_API_KEY=xxxxx で API キーを安全に設定

import os
import sys
import json
from inference_sdk import InferenceHTTPClient

API_KEY = os.getenv("ROBOFLOW_API_KEY", "zxCjDibvvTfkYK30nT45")  # 本番は環境変数で上書き
MODEL_ID = "plant_care/9"
DEFAULT_IMAGE_PATH = os.getenv(
    "PLANT_CARE_IMAGE", "images/Okra02-min.jpg")


def main():
    if len(sys.argv) < 2:
        image_path = DEFAULT_IMAGE_PATH
        print(f"[INFO] 引数未指定のためデフォルト画像を使用: {image_path}")
    else:
        image_path = sys.argv[1]
    if not os.path.exists(image_path):
        print(f"画像が見つかりません: {image_path}", file=sys.stderr)
        sys.exit(1)

    client = InferenceHTTPClient(
        api_url="https://serverless.roboflow.com", api_key=API_KEY)
    try:
        result = client.infer(image_path, model_id=MODEL_ID)
    except Exception as e:  # noqa: BLE001
        print(f"推論失敗: {e!r}", file=sys.stderr)
        sys.exit(1)

    # 生結果確認 (必要ならコメントアウト)
    print("=== RAW RESULT ===")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("===================")

    preds = result.get("predictions", []) or []
    if not preds:
        print("検出なし")
        return

    for p in preds:
        cls = p.get("class")
        conf = p.get("confidence", 0.0)
        print(f"{cls} confidence={conf:.3f}")


if __name__ == "__main__":  # pragma: no cover
    main()
