"""web_search_preview を任意キーワードで叩いて生出力を検証する単体スクリプト。

例:
  python confirmetion/web_search_keyword_probe.py --query "東京 観光 名所" --json
  python confirmetion/web_search_keyword_probe.py --query "半導体 市況" --prompt "最新の要点を3行で: {query}" --strip-links

出力:
  --raw 指定: 取得した生テキスト(先頭 4000 文字)
  通常: 解析結果 (best-effort JSON パース) + raw_head/raw_len + Raw 本文(先頭 1000 or 4000)

環境変数:
  PROBE_SEARCH_TIMEOUT  タイムアウト秒 (default 10.0)
  OPENAI_API_KEY        OpenAI クライアント用

備考:
  - --prompt でテンプレート指定 ( {query} が置換 ) 未指定ならクエリそのまま
  - JSON schema は付与せず自由形式を観察
  - --strip-links で URL / 参照番号 等を簡易除去
  - 504 相当 timeout / 502 相当 upstream エラーで exit code 1
"""
import argparse
import asyncio
import json
import logging
import os
import re
import sys
from typing import Any, Dict
from pathlib import Path

# プロジェクトルート(app がある位置) を sys.path に追加
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.openai_client import client  # noqa: E402

logger = logging.getLogger("probe")
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

SEARCH_TIMEOUT = float(os.getenv("PROBE_SEARCH_TIMEOUT", "10.0"))

# プリセットプロンプトテンプレート (ユーザー入力本文生成用) {query}
TEMPLATES = {
    "simple": "{query}",
    "news_summary": "以下の検索キーワードに関する直近の主要ポイントを簡潔に日本語要約100文字程度にまとめてほしい: {query}",
    "constraint": "URL/引用番号の出力は禁止、**最新情報をから必ず**回答を生成すること: {query}",
}

# system instructions 用テンプレ {query} {bullets}
INSTR_TEMPLATES = {
    "plain": "{query} について簡潔に回答。URL や 出典, 参照番号, リンクは一切含めない。",
    "summary": "以下のトピックを日本語で約120文字の要約: {query}。URL/出典/参照番号禁止。",
    "bullets": "トピック: {query}\n主要ポイントを{bullets}項目 箇条書き。各行60字以内。番号, URL, 出典, 参照記号は禁止。",
    "risk": "{query} に関する主要なリスクを3項目。各リスク1行。URL/出典/参照番号禁止。",
    "explain": "{query} を初心者向けに平易な日本語で短く説明。URL/出典/参照番号/脚注禁止。",
}


def _safe_json(text: str) -> Dict[str, Any]:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t[:-3]
    if "{" in t and "}" in t:
        t = t[t.find("{"): t.rfind("}") + 1]
    t = re.sub(r"[\x00-\x1F\x7F]", "", t)
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        return {}


async def run_search(input_text: str, instructions: str, model: str, orig_query: str) -> Dict[str, Any]:
    try:
        resp = await asyncio.wait_for(
            client.responses.create(
                model=model,
                input=input_text,
                instructions=instructions,
                tools=[{"type": "web_search_preview"}],
                tool_choice={"type": "web_search_preview"},
            ),
            timeout=SEARCH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        print(f"[ERROR] timeout (>= {SEARCH_TIMEOUT:.1f}s)", file=sys.stderr)
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] upstream error: {e!r}", file=sys.stderr)
        sys.exit(1)

    output_text = (getattr(resp, "output_text", None) or "").strip()
    parsed = _safe_json(output_text)
    return {
        "parsed": parsed if parsed else None,
        "raw_text": output_text,
        "raw_head": output_text[:200],
        "raw_len": len(output_text),
        "ok_json": bool(parsed),
        "query": orig_query,
        "input_used": input_text,
        "instructions_used": instructions,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="web_search_preview キーワード検索検証ツール")
    p.add_argument("--query", help="検索キーワード。未指定なら起動後に入力プロンプトを表示")
    p.add_argument(
        "--prompt", help="送信プロンプトテンプレート。{query} が置換。未指定ならクエリをそのまま送信")
    p.add_argument("--model", default="gpt-4o-mini", help="利用モデル")
    p.add_argument("--raw", action="store_true", help="生テキスト全量を表示")
    p.add_argument("--json", action="store_true", help="結果 dict を JSON 形式で出力")
    p.add_argument("--strip-links", action="store_true",
                   help="URL/参照番号/Markdownリンクを除去して表示")
    p.add_argument("--template", choices=list(TEMPLATES.keys()),
                   help="事前定義テンプレート名 (--prompt より低優先)")
    p.add_argument("--instructions",
                   help="system instructions を直接指定 ( {query} {bullets} 利用可 )")
    p.add_argument("--instr-template", choices=list(INSTR_TEMPLATES.keys()),
                   help="事前定義 instructions テンプレ名 (--instructions より低優先)")
    p.add_argument("--bullets", type=int, default=5,
                   help="bullets テンプレ使用時の項目数")
    p.add_argument("--max-chars", type=int, default=0,
                   help="最終出力テキストをこの文字数で丸める(0で無効)")
    return p.parse_args()


def strip_citations(s: str) -> str:
    import re as _re
    s = _re.sub(r"https?://\S+", "", s)
    s = _re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", s)  # markdown link -> テキスト
    s = _re.sub(r"\[(?:\d+|ref?)\]", "", s, flags=_re.I)  # [1] / [ref]
    s = _re.sub(r"^\s*\[(?:\d+|\w+)\].*$", "", s, flags=_re.M)  # 行頭参照
    s = _re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def main():
    args = parse_args()
    query = args.query
    if not query:
        try:
            query = input("検索キーワードを入力してください: ").strip()
        except EOFError:  # 非対話環境
            print("[ERROR] キーワードが指定されていません (--query か対話入力)", file=sys.stderr)
            sys.exit(1)
    if not query:
        print("[ERROR] 空のキーワード", file=sys.stderr)
        sys.exit(1)

    # プロンプト生成 優先順位: --prompt > --template > simple
    if args.prompt:
        template = args.prompt
    elif args.template:
        template = TEMPLATES[args.template]
    else:
        template = TEMPLATES["simple"]
    try:
        input_text = template.format(query=query)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] テンプレートフォーマット失敗: {e}", file=sys.stderr)
        sys.exit(1)

    # instructions 生成 優先順位: --instructions > --instr-template > plain
    if args.instructions:
        instr_template = args.instructions
        instr_name = "custom"
    elif args.instr_template:
        instr_template = INSTR_TEMPLATES[args.instr_template]
        instr_name = args.instr_template
    else:
        instr_template = INSTR_TEMPLATES["plain"]
        instr_name = "plain"
    try:
        instructions_text = instr_template.format(
            query=query, bullets=args.bullets)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] instructions フォーマット失敗: {e}", file=sys.stderr)
        sys.exit(1)

    result = asyncio.run(run_search(
        input_text, instructions_text, args.model, query))

    # strip-links は表示前に適用
    if args.strip_links:
        result["raw_text"] = strip_citations(result["raw_text"])
        result["raw_head"] = result["raw_text"][:200]

    # 文字数丸め
    if args.max_chars and args.max_chars > 0:
        if len(result["raw_text"]) > args.max_chars:
            result["raw_text"] = result["raw_text"][: args.max_chars]
            result["raw_head"] = result["raw_text"][:200]

    if args.raw:
        print(result["raw_text"])  # raw は全量(丸め後)
        return

    if args.json:
        out = dict(result)
        out.pop("raw_text", None)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        # 検索結果本文のみ出力
        print(result['raw_text'])


if __name__ == "__main__":  # pragma: no cover
    main()
