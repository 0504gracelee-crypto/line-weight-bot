import os
import re
import json
from datetime import datetime, timedelta
import pytz
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient, Configuration, MessagingApi,
    ReplyMessageRequest, PushMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# ===== 設定區 =====
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
USER_MAP_SHEET = "用戶對照表"
TZ = pytz.timezone("Asia/Taipei")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ===== 欄位定義 =====
# 日期、體重、水份、蛋白質、蔬菜、澱粉、營養素、運動、睡眠、備註、建立時間
COLUMNS = {
    "date":      1,
    "weight":    2,   # 數值（kg）
    "water":     3,   # y/n
    "protein":   4,   # y/n
    "veggie":    5,   # y/n
    "fruit":     6,   # y/n
    "carb":      7,   # y/n
    "nutrient":  8,   # y/n
    "exercise":  9,   # 數值（分鐘）
    "sleep":     10,  # 數值（小時）
    "note":      11,  # 文字
    "created":   12,  # 建立時間
}

HEADER = ['日期', '體重', '水份', '蛋白質', '蔬菜', '水果', '澱粉', '營養素', '運動(分鐘)', '睡眠(小時)', '備註', '建立時間']

LABEL_MAP = {
    "weight":   "體重",
    "water":    "水份",
    "protein":  "蛋白質",
    "veggie":   "蔬菜",
    "fruit":    "水果",
    "carb":     "澱粉",
    "nutrient": "營養素",
    "exercise": "運動",
    "sleep":    "睡眠",
    "note":     "備註",
}

YN_KEYS = {"water", "protein", "veggie", "fruit", "carb", "nutrient"}
YN_KEYWORDS = {
    "水份": "water",
    "蛋白質": "protein",
    "蔬菜": "veggie",
    "水果": "fruit",
    "澱粉": "carb",
    "營養素": "nutrient",
}

# ===== Google Sheets 連線 =====
def get_gspread_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    elif os.path.exists("credentials.json"):
        creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
    else:
        raise RuntimeError("找不到 Google 憑證，請設定 GOOGLE_CREDENTIALS_JSON 環境變數")
    return gspread.authorize(creds)

# ===== 用戶對照表 =====
def get_user_map_sheet(client):
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    sheet_names = [ws.title for ws in spreadsheet.worksheets()]
    if USER_MAP_SHEET not in sheet_names:
        ws = spreadsheet.add_worksheet(title=USER_MAP_SHEET, rows=200, cols=2)
        ws.update(range_name='A1:B1', values=[['user_id', '名字']])
        return ws
    return spreadsheet.worksheet(USER_MAP_SHEET)

def get_name_by_userid(client, user_id):
    ws = get_user_map_sheet(client)
    all_ids = ws.col_values(1)
    if user_id in all_ids:
        row = all_ids.index(user_id) + 1
        return ws.cell(row, 2).value
    return None

def get_all_users(client):
    ws = get_user_map_sheet(client)
    all_ids = ws.col_values(1)
    all_names = ws.col_values(2)
    users = []
    for i, uid in enumerate(all_ids):
        if uid == "user_id" or not uid:
            continue
        name = all_names[i] if i < len(all_names) else ""
        users.append({"user_id": uid, "name": name})
    return users

def save_user_name(client, user_id, name):
    ws = get_user_map_sheet(client)
    all_ids = ws.col_values(1)
    if user_id in all_ids:
        row = all_ids.index(user_id) + 1
        ws.update_cell(row, 2, name)
    else:
        next_row = len(all_ids) + 1
        ws.update_cell(next_row, 1, user_id)
        ws.update_cell(next_row, 2, name)

# ===== 學員工作表 =====
def get_or_create_sheet(client, name):
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    sheet_names = [ws.title for ws in spreadsheet.worksheets()]
    if name in sheet_names:
        return spreadsheet.worksheet(name)
    new_sheet = spreadsheet.add_worksheet(title=name, rows=1000, cols=13)
    new_sheet.update(range_name='A1:L1', values=[HEADER])
    return new_sheet

def get_or_create_today_row(sheet):
    today = datetime.now(TZ).strftime("%Y/%m/%d")
    all_dates = sheet.col_values(1)
    if today in all_dates:
        return all_dates.index(today) + 1, False
    next_row = len(all_dates) + 1
    now_str = datetime.now(TZ).strftime("%Y/%m/%d %H:%M")
    sheet.update_cell(next_row, COLUMNS["date"], today)
    sheet.update_cell(next_row, COLUMNS["created"], now_str)
    return next_row, True

def has_filled_today(sheet):
    today = datetime.now(TZ).strftime("%Y/%m/%d")
    all_dates = sheet.col_values(1)
    if today not in all_dates:
        return False
    row_index = all_dates.index(today) + 1
    row_data = sheet.row_values(row_index)
    for col in range(2, 12):
        val = row_data[col - 1] if len(row_data) >= col else ""
        if str(val).strip():
            return True
    return False

# ===== 解析訊息 =====
def parse_message(text):
    text = text.strip()

    # y/n 打卡
    if text in YN_KEYWORDS:
        return {YN_KEYWORDS[text]: "y"}

    # 運動（分鐘）
    m = re.match(r"^運動\s*(\d+(\.\d+)?)$", text)
    if m:
        return {"exercise": m.group(1)}

    # 睡眠（小時）
    m = re.match(r"^睡眠\s*(\d+(\.\d+)?)$", text)
    if m:
        return {"sleep": m.group(1)}

    # 備註
    m = re.match(r"^備註\s+(.+)$", text)
    if m:
        return {"note": m.group(1)}

    # 體重
    m = re.match(r"^(?:體重\s*)?(\d+(\.\d+)?)$", text)
    if m:
        return {"weight": m.group(1)}

    return None

def parse_modify(text):
    text = text.strip()

    # 修改 y/n 項目
    for kw, key in YN_KEYWORDS.items():
        m = re.match(rf"^修改{kw}\s*([yn])$", text)
        if m:
            return {key: m.group(1)}

    # 修改數值項目
    patterns = {
        r"^修改體重\s*(\d+(\.\d+)?)$": "weight",
        r"^修改運動\s*(\d+(\.\d+)?)$": "exercise",
        r"^修改睡眠\s*(\d+(\.\d+)?)$": "sleep",
        r"^修改備註\s+(.+)$":          "note",
    }
    for pattern, key in patterns.items():
        m = re.match(pattern, text)
        if m:
            return {key: m.group(1)}

    return None

# ===== 格式化顯示 =====
def format_value(key, val):
    if key in YN_KEYS:
        return "✅ 達成" if val == "y" else "❌ 取消"
    if key == "weight":
        return f"{val} kg"
    if key == "exercise":
        return f"{val} 分鐘"
    if key == "sleep":
        return f"{val} 小時"
    return val

def update_sheet(sheet, row_index, data):
    for key, col in COLUMNS.items():
        if key in data:
            sheet.update_cell(row_index, col, data[key])

def build_reply(data, is_new_row, name):
    lines = [f"📝 {name} 今天的紀錄："]
    for key, val in data.items():
        lines.append(f"  {LABEL_MAP.get(key, key)}：{format_value(key, val)}")
    if is_new_row:
        lines.append("\n（今天第一筆，已自動建立新列）")
    return "\n".join(lines)

def build_modify_reply(data, name):
    lines = [f"✏️ {name} 今天的資料已修改："]
    for key, val in data.items():
        lines.append(f"  {LABEL_MAP.get(key, key)}：{format_value(key, val)}")
    return "\n".join(lines)

# ===== 23點提醒推播 =====
@app.route("/remind", methods=["GET"])
def remind():
    client = get_gspread_client()
    users = get_all_users(client)
    reminded = []

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        for user in users:
            user_id = user["user_id"]
            name = user["name"]
            if not name:
                continue
            try:
                sheet = get_or_create_sheet(client, name)
                if not has_filled_today(sheet):
                    msg = (
                        f"⏰ {name}，今天還沒填記錄喔！\n\n"
                        f"現在還來得及，記得在 24:00 前填完：\n\n"
                        f"  體重：輸入數字，例如 63\n"
                        f"  水份／蛋白質／蔬菜／水果／澱粉／營養素：輸入對應關鍵字\n"
                        f"  運動：例如「運動60」（分鐘）\n"
                        f"  睡眠：例如「睡眠8」（小時）\n"
                        f"  備註：例如「備註 今天吃少一點」\n\n"
                        f"加油！你可以的 💪"
                    )
                    line_bot_api.push_message(
                        PushMessageRequest(
                            to=user_id,
                            messages=[TextMessage(text=msg)]
                        )
                    )
                    reminded.append(name)
            except Exception as e:
                print(f"提醒 {name} 失敗：{e}")

    return f"已提醒：{', '.join(reminded) if reminded else '所有人都填完了 🎉'}", 200

# ===== 昨日補空值 =====
@app.route("/fill_yesterday", methods=["GET"])
def fill_yesterday():
    client = get_gspread_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    yesterday = (datetime.now(TZ) - timedelta(days=1)).strftime("%Y/%m/%d")
    results = []

    for ws in spreadsheet.worksheets():
        if ws.title == USER_MAP_SHEET:
            continue
        all_dates = ws.col_values(1)
        if yesterday not in all_dates:
            continue
        row_index = all_dates.index(yesterday) + 1
        row_data = ws.row_values(row_index)

        def get_col(i):
            return row_data[i] if len(row_data) > i else ""

        updates = []
        if get_col(COLUMNS["weight"] - 1) == "":
            updates.append((row_index, COLUMNS["weight"], 0))
        for key in ("water", "protein", "veggie", "carb", "nutrient"):
            if get_col(COLUMNS[key] - 1) == "":
                updates.append((row_index, COLUMNS[key], "n"))
        for key in ("exercise", "sleep"):
            if get_col(COLUMNS[key] - 1) == "":
                updates.append((row_index, COLUMNS[key], 0))

        for r, c, v in updates:
            ws.update_cell(r, c, v)
        results.append(ws.title)

    return f"已補齊：{', '.join(results) if results else '無需補齊'}", 200

# ===== LINE Webhook =====
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

HELP_TEXT = (
    "📌 記錄指令：\n"
    "  體重：輸入數字，例如 63\n"
    "  水份／蛋白質／蔬菜／水果／澱粉／營養素：輸入對應關鍵字\n"
    "  運動：例如「運動60」（分鐘）\n"
    "  睡眠：例如「睡眠8」（小時）\n"
    "  備註：例如「備註 今天吃少一點」\n\n"
    "✏️ 修改今天的資料：\n"
    "  例如「修改體重 65」\n"
    "  例如「修改水份 n」\n"
    "  例如「修改運動 45」\n"
    "  例如「修改睡眠 7」\n"
    "  例如「修改備註 今天吃太多」"
)

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    text = event.message.text.strip()
    user_id = event.source.user_id
    client = get_gspread_client()

    # 設定名字
    name_match = re.match(r"^名字\s*(.+)$", text)
    if name_match:
        name = name_match.group(1).strip()
        save_user_name(client, user_id, name)
        get_or_create_sheet(client, name)
        reply_text = f"✅ 已建立「{name}」的紀錄表！\n\n現在可以開始記錄了～\n\n{HELP_TEXT}"
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message_with_http_info(
                ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_text)])
            )
        return

    # 查詢名字
    name = get_name_by_userid(client, user_id)
    if name is None:
        reply_text = "👋 歡迎！請先設定你的名字來建立紀錄表：\n\n輸入「名字 你的名字」\n例如：名字 小明"
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message_with_http_info(
                ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_text)])
            )
        return

    # 記錄或修改
    modify_data = parse_modify(text)
    parsed = parse_message(text) if not modify_data else None

    if modify_data is None and parsed is None:
        reply_text = f"❓ 我看不懂這個指令，請用以下格式：\n\n{HELP_TEXT}"
    else:
        sheet = get_or_create_sheet(client, name)
        row_index, is_new_row = get_or_create_today_row(sheet)
        if modify_data:
            update_sheet(sheet, row_index, modify_data)
            reply_text = build_modify_reply(modify_data, name)
        else:
            update_sheet(sheet, row_index, parsed)
            reply_text = build_reply(parsed, is_new_row, name)

    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message_with_http_info(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_text)])
        )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
