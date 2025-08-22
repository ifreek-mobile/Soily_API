from openai import OpenAI
import os
from dotenv import load_dotenv

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

schema = {
    "type": "object",
    "properties": {
        "city": {"type": "string", "description": "特定した都市名"},
        "weather": {"type": "string", "description": "本日の天気情報（晴れ、曇り、雨、雷、雪、時々〇〇）"},
    },
    "required": ["city", "weather"],
    "additionalProperties": False,
    "strict": True,
}

def main():
    response = client.responses.create(
        model="gpt-4o-mini",
        input="緯度{latitude} 経度{longitude}から場所の特定と本日の天気の情報を取得して".format(latitude=35.7972, longitude=139.5939),
        tools=[{"type": "web_search_preview"}],
        tool_choice={"type": "web_search_preview"},
        text={
            "format": {
                "type": "json_schema",
                "name": "WeatherJson",
                "schema": schema,
            }
        },
    )
    print(response.output_text)

if __name__ == "__main__":
    main()
