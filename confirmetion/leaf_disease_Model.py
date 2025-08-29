# leaf_disease_Model.py
# 依存:
#   pip install inference-sdk
# 使い方例:
#   python confirmetion/leaf_disease_Model.py images/Okra02-min.jpg
#   ROBOFLOW_API_KEY を環境変数に設定すると安全 (未設定時はサンプルキー使用)

import os
import sys
from inference_sdk import InferenceHTTPClient
import json  # 追加


API_KEY = os.getenv("ROBOFLOW_API_KEY", "zxCjDibvvTfkYK30nT45")  # 本番は必ず環境変数で管理
MODEL_ID = "leaf-disease-nsdsr/1"
DEFAULT_IMAGE_PATH = "images/Okra02-min.jpg"  # 事前指定パス（環境変数で上書き可）


def main():
    if len(sys.argv) < 2:
        image_path = os.getenv("LEAF_IMAGE_PATH", DEFAULT_IMAGE_PATH)
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

    # 追加: 生の返却内容を整形表示
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
