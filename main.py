import os
import re
import json
import urllib.request
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,  # ★ Push送信のために追加
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

# 処理ロジック (変更なし)
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

    if not raw_content or len(raw_content.strip()) < 20:
        print(f"[WARN] Failed to fetch content from URL: {url}. Skipping DB save.")
        return f"⚠️ ページの本文を取得できなかったため、保存をスキップしました。\n🔗 {url}"

    # --- Step 3: OpenAI で解析 ---
    prompt = f"""
以下のWebページの内容から『料理名(title)』と『使用されている主な食材・分量付きの調味料・味付け(ingredients)』を抽出して、指定のJSON形式で返してください。

【出力フォーマット】
{{
  "title": "料理名",
  "ingredients": ["醤油 大さじ2", "みりん 大さじ1", "砂糖 小さじ1", "おろし生姜 1片"]
}}

【抽出・整形ルール】
1. 『味付け・調味料』には、料理の味を決める調味料・香辛料・タレ（醤油、みりん、塩、ニンニク、姜など）のみを含めてください。
2. 【絶対除外ルール】以下のものは「味付け」ではないため、絶対に抽出リストに入れないでください。
   - 水、湯、ぬるま湯
   - サラダ油、ごま油、オリーブオイル、サラダ油（炒め用）などの油脂類（風味付けのスパイスオイルを除く）
   - 片栗粉、薄力粉、小麦粉（とろみ付けや衣用）
3. 【グループ展開ルール】「A: 醤油大さじ1、A: 酒大さじ1」や「合わせ調味料」のようにまとめられている場合、「A」などのグループ記号は取り除き、中の調味料（醤油、酒）を個別の要素として抽出してください。

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

    # --- Step 4: バリデーション ---
    if not title or title in ["不明なレシピ", "取得失敗レシピ", "解析エラーレシピ"] or not ingredients:
        print(f"[WARN] Incomplete recipe data (title: '{title}', ingredients: '{ingredients}'). Skipping DB save.")
        return f"⚠️ レシピ名または食材情報の抽出に失敗したため、保存をスキップしました。\n🔗 {url}"

    save_recipe_to_db(title, url, ingredients)
    
    return f"【レシピを保存しました！】\n📖 {title}\n🛒 食材: {ingredients}\n🔗 {url}"

def process_search_recipes(user_query: str) -> str:
    favorites = get_all_recipes_from_db()
    
    fav_text = "\n".join([
        f"- タイトル: {r.get('title', '')} | 味付け・食材: {r.get('ingredients', '')} | URL: {r.get('url', '')}" 
        for r in favorites
    ])
    
    search_res = tavily_client.search(query=f"{user_query} レシピ", max_results=3)
    results_list = search_res.get('results', [])
    results_text = "\n".join([
        f"・タイトル: {r.get('title', '')}\n  URL: {r.get('url', '')}\n  概要: {r.get('content', '')[:100]}" 
        for r in results_list
    ])
    
    prompt = f"""ユーザーの入力条件「{user_query}」に対して、「過去のお気に入り」と「Web検索結果」の双方から最適なレシピを提案してください。

【ユーザーの検索条件】
{user_query}

【登録済みお気に入りレシピ】
{fav_text if fav_text else "まだ登録なし"}

【最新のWeb検索結果】
{results_text}

【出力・提案ルール】
1. 以下の2つのセクションに分けて提案を作成してください。
   ・「📁 お気に入りからの提案」: 【登録済みお気に入りレシピ】の中から条件に合うもの（該当がなければ省略可）。
   ・「🌐 新しいWebレシピ」: 【最新のWeb検索結果】の中から条件に合うもの。
2. 過去のお気に入りレシピの「味付け・分量」の傾向を考慮し、ユーザーが好みそうなポイントを添えてください。
3. **【最重要】提案するすべてのレシピに、絶対に正確な URL を記載してください。** URLのない提案は厳禁です。LINEでタップしやすいよう、URLは独立した行に出力してください。

【出力フォーマット例】
「{user_query}」のおすすめレシピです！

📁 **お気に入りから**
・料理名
ポイント: いつもの甘辛い味付けです。
URL:
https://...

🌐 **新しいWebレシピ**
・料理名
ポイント: 時短で作れるアイデアレシピです。
URL:
https://...
"""

    response = openai_client.chat.completions.create(
        model="gpt-5-nano",
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content


# ★ 重い AI/DB 検索処理をバックグラウンドで実行して Push メッセージを送る新関数
def process_message_async(user_text: str, target_id: str):
    urls = re.findall(r'https?://[^\s]+', user_text)

    try:
        if urls:
            reply_text = process_add_recipe(urls[0])
        elif user_text.lower() in ["使い方", "ヘルプ", "help"]:
            reply_text = "【使い方】\n・レシピのURLを送るとDBに保存します。\n・食材名や「時短レシピ」などの条件を送るとAIが提案します。"
        else:
            reply_text = process_search_recipes(user_text)

        # 完成した結果を Push メッセージ（to=target_id）で直接送信
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.push_message(
                PushMessageRequest(
                    to=target_id,
                    messages=[TextMessage(text=reply_text)]
                )
            )
    except Exception as e:
        print(f"[ERROR] Async process failed: {e}")


@app.get("/")
def root():
    return {"status": "ok"}


# ★ バックグラウンドタスク（BackgroundTasks）を受け取る形式に変更
@app.post("/callback")
async def callback(request: Request, background_tasks: BackgroundTasks):
    signature = request.headers.get("X-Line-Signature", "")
    body = (await request.body()).decode("utf-8")
    
    try:
        events = handler.parser.parse(body, signature)
        for event in events:
            if isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
                # イベントをハンドラーへ引き渡す際に background_tasks も受け渡す
                handle_message(event, background_tasks)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "OK"


# ★ メッセージ受け取り時に即座に「一次返信」を行い、バックグラウンドにタスク追加
def handle_message(event: MessageEvent, background_tasks: BackgroundTasks):
    user_text = event.message.text.strip()
    
    # 個人チャット(user)でもグループLINE(group/room)でも送信できるように対象IDを取得
    target_id = event.source.user_id if event.source.type == "user" else getattr(event.source, f"{event.source.type}_id", event.source.user_id)

    # 1. まず「受け取りました」のメッセージを即座に ReplyToken で返信 (0.1秒)
    quick_ack_text = "リクエストを受け取りました！レシピを準備中ですので少々お待ちください... 🔍"
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=quick_ack_text)]
                )
            )
    except Exception as e:
        print(f"[ERROR] Failed to send quick reply: {e}")

    # 2. 時間のかかる本処理をバックグラウンドへ追加（非同期実行）
    background_tasks.add_task(process_message_async, user_text, target_id)
