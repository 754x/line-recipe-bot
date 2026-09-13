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
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "").strip()
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()

# クライアント初期化
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
openai_client = OpenAI(api_key=OPENAI_API_KEY, timeout=30.0, max_retries=3)
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
    raw_content = ""
    
    # --- Step 1: Tavily で抽出試行 ---
    try:
        search_res = tavily_client.extract(urls=[url])
        results = search_res.get('results', [])
        if results and results[0].get('raw_content'):
            raw_content = results[0].get('raw_content', '')
            print(f"[DEBUG] Tavily Extract succeeded. Length: {len(raw_content)}")
    except Exception as e:
        print(f"[DEBUG] Tavily Extract Error: {e}")

    # --- Step 2: Tavily で取得できなかった場合のフォールバック（HTML取得） ---
    if not raw_content or len(raw_content.strip()) < 50:
        print("[DEBUG] Falling back to standard HTTP request...")
        try:
            req = urllib.request.Request(
                url, 
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            )
            with urllib.request.urlopen(req, timeout=10) as res:
                html = res.read().decode('utf-8', errors='ignore')
                soup = BeautifulSoup(html, 'html.parser')
                for script in soup(["script", "style"]):
                    script.decompose()
                raw_content = soup.get_text(separator=' ', strip=True)
                print(f"[DEBUG] Fallback HTTP succeeded. Length: {len(raw_content)}")
        except Exception as e:
            print(f"[DEBUG] Fallback HTTP Error: {e}")

    # 本文が全く取得できなかった場合は保存せずに中断
    if not raw_content or len(raw_content.strip()) < 20:
        print(f"[WARN] Failed to fetch content from URL: {url}. Skipping DB save.")
        return f"⚠️ ページの本文を取得できなかったため、保存をスキップしました。\n🔗 {url}"

    # --- Step 3: OpenAI で解析 ---
    prompt = f"""
以下のWebページの内容から『料理名(title)』と『使用されている主な食材・調味料(ingredients)』を抽出して、指定のJSON形式で返してください。

【出力フォーマット】
{{
  "title": "料理名",
  "ingredients": ["食材1", "食材2", "調味料1"]
}}

【Webページ内容】
{raw_content[:4000]}
"""

    title = ""
    ingredients = ""

    try:
        response = openai_client.chat.completions.create(
            model="gpt-5-nano",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        res_text = response.choices[0].message.content
        print(f"[DEBUG] OpenAI Raw Response: {res_text}")
        
        data = json.loads(res_text)
        title = str(data.get("title", "")).strip()
        
        ingredients_raw = data.get("ingredients", [])
        if isinstance(ingredients_raw, list):
            ingredients = ", ".join([str(x).strip() for x in ingredients_raw if str(x).strip()])
        else:
            ingredients = str(ingredients_raw).strip()
            
    except Exception as e:
        print(f"[ERROR] OpenAI / JSON Parsing Failed: {e}")

    # --- Step 4: バリデーション（取得失敗時は DB 保存しない） ---
    if not title or title in ["不明なレシピ", "取得失敗レシピ", "解析エラーレシピ"] or not ingredients:
        print(f"[WARN] Incomplete recipe data (title: '{title}', ingredients: '{ingredients}'). Skipping DB save.")
        return f"⚠️ レシピ名または食材情報の抽出に失敗したため、保存をスキップしました。\n🔗 {url}"

    # 正常に取得できた場合のみ Supabase へ保存
    save_recipe_to_db(title, url, ingredients)
    
    return f"【レシピを保存しました！】\n📖 {title}\n🛒 食材: {ingredients}\n🔗 {url}"

def process_search_recipes(user_query: str) -> str:
    favorites = get_all_recipes_from_db()
    fav_text = "\n".join([f"- {r['title']}: {r['ingredients']}" for r in favorites])
    
    search_res = tavily_client.search(query=f"{user_query} レシピ", max_results=3)
    results_text = "\n".join([f"・{r['title']}\n  URL: {r['url']}" for r in search_res.get('results', [])])
    
    prompt = f"お気に入り傾向:\n{fav_text}\n\n検索結果:\n{results_text}\n\nユーザー指定条件: {user_query}\n\n上記を元にお気に入りの傾向を踏まえたおすすめレシピを簡潔に提案してください。"
    response = openai_client.chat.completions.create(
        model="gpt-5-nano",
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
