import os
import re
from fastapi import FastAPI, Request, HTTPException
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from supabase import create_client, Client
from openai import OpenAI
from tavily import TavilyClient

app = FastAPI()

# 環境変数の読み込み
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# クライアント初期化
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
openai_client = OpenAI(api_key=OPENAI_API_KEY)
tavily_client = TavilyClient(api_key=TAVILY_API_KEY)

# DBヘルパー関数
def save_recipe_to_db(title: str, url: str, ingredients: str):
    data = {"title": title, "url": url, "ingredients": ingredients}
    supabase.table("favorite_recipes").upsert(data, on_conflict="url").execute()

def get_all_recipes_from_db():
    res = supabase.table("favorite_recipes").select("*").execute()
    return res.data or []

# 処理ロジック
def process_add_recipe(url: str) -> str:
    search_res = tavily_client.extract(urls=[url])
    raw_content = search_res['results'][0]['raw_content'] if search_res['results'] else ""
    
    prompt = f"以下のWebページから『料理タイトル』と『主な食材・調味料』を抽出し、JSON形式で返してください。\n内容:\n{raw_content[:2000]}"
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"}
    )
    import json
    data = json.loads(response.choices[0].message.content)
    title = data.get("title", "不明なレシピ")
    ingredients = ", ".join(data.get("ingredients", [])) if isinstance(data.get("ingredients"), list) else str(data.get("ingredients", ""))
    
    save_recipe_to_db(title, url, ingredients)
    return f"【レシピを保存しました！】\n📖 {title}\n🔗 {url}"

def process_search_recipes(user_query: str) -> str:
    favorites = get_all_recipes_from_db()
    fav_text = "\n".join([f"- {r['title']}: {r['ingredients']}" for r in favorites])
    
    search_res = tavily_client.search(query=f"{user_query} レシピ", max_results=3)
    results_text = "\n".join([f"・{r['title']}\n  URL: {r['url']}" for r in search_res.get('results', [])])
    
    prompt = f"お気に入り傾向:\n{fav_text}\n\n検索結果:\n{results_text}\n\nユーザー指定条件: {user_query}\n\n上記を元にお気に入りの傾向を踏まえたおすすめレシピを簡潔に提案してください。"
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content

@app.get("/")
def root():
    return {"status": "ok"}

@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = (await request.body()).decode("utf-8")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_text = event.message.text.strip()
    urls = re.findall(r'https?://[^\s]+', user_text)

    try:
        if urls:
            reply_text = process_add_recipe(urls[0])
        elif user_text in ["使い方", "ヘルプ", "help"]:
            reply_text = "【使い方】\n・レシピのURLを送るとDBに保存します。\n・食材名や「時短レシピ」などの条件を送るとAIが提案します。"
        else:
            reply_text = process_search_recipes(user_text)

        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )
    except Exception as e:
        print(f"Error: {e}")
