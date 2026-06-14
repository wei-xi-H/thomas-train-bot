import os
import requests
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage, QuickReply, QuickReplyItem,
    MessageAction
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = Flask(__name__)

# 環境變數
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
TDX_CLIENT_ID = os.environ.get("TDX_CLIENT_ID")
TDX_CLIENT_SECRET = os.environ.get("TDX_CLIENT_SECRET")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 常用大站
POPULAR_STATIONS = ["台北", "板橋", "桃園", "新竹", "台中", "嘉義", "台南", "高雄", "花蓮", "台東"]

# 用戶狀態暫存（記錄選站流程）
user_state = {}

# ── TDX 取得 Access Token ──────────────────────────────────────────
def get_tdx_token():
    url = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": TDX_CLIENT_ID,
        "client_secret": TDX_CLIENT_SECRET,
    }
    res = requests.post(url, data=data)
    return res.json().get("access_token")

# ── 查詢時刻表 ────────────────────────────────────────────────────
def get_timetable(origin, destination, date_str):
    token = get_tdx_token()
    headers = {"Authorization": f"Bearer {token}"}
    url = (
        f"https://tdx.transportdata.tw/api/basic/v3/Rail/TRA/DailyTrainTimetable/OD"
        f"/{origin}/{destination}/{date_str}"
        f"?%24top=5&%24format=JSON"
    )
    res = requests.get(url, headers=headers)
    if res.status_code != 200:
        return None
    data = res.json()
    trains = data.get("TrainTimetables", [])
    return trains[:5]

# ── 查詢即時動態（誤點） ──────────────────────────────────────────
def get_live_delay(train_no, station_id):
    token = get_tdx_token()
    headers = {"Authorization": f"Bearer {token}"}
    url = (
        f"https://tdx.transportdata.tw/api/basic/v3/Rail/TRA/LiveBoard/Station/{station_id}"
        f"?%24format=JSON"
    )
    res = requests.get(url, headers=headers)
    if res.status_code != 200:
        return None
    items = res.json().get("TrainLiveBoards", [])
    for item in items:
        if str(item.get("TrainNo")) == str(train_no):
            return item.get("DelayTime", 0)
    return None

# ── 格式化結果訊息 ────────────────────────────────────────────────
def format_result(trains, origin, destination, date_label):
    if not trains:
        return f"😕 找不到 {origin} → {destination} 的班次\n請確認站名是否正確"

    lines = [f"🚂 {origin} → {destination}｜{date_label}班次\n{'─' * 20}"]
    for t in trains:
        info = t.get("TrainInfo", {})
        stops = t.get("StopTimes", [])

        train_no = info.get("TrainNo", "")
        train_type = info.get("TrainTypeName", {}).get("Zh_tw", "")

        dep = next((s for s in stops if s.get("StationName", {}).get("Zh_tw") == origin), None)
        arr = next((s for s in stops if s.get("StationName", {}).get("Zh_tw") == destination), None)

        if not dep or not arr:
            continue

        dep_time = dep.get("DepartureTime", "--:--")
        arr_time = arr.get("ArrivalTime", "--:--")

        # 計算行程時間
        try:
            d = datetime.strptime(dep_time, "%H:%M")
            a = datetime.strptime(arr_time, "%H:%M")
            if a < d:
                a += timedelta(days=1)
            mins = int((a - d).total_seconds() / 60)
            duration = f"{mins // 60}h{mins % 60:02d}m"
        except Exception:
            duration = ""

        lines.append(f"{train_type} {train_no}｜{dep_time} → {arr_time}（{duration}）✅")

    lines.append("\n查明天請傳：台北 高雄 明天")
    return "\n".join(lines)

# ── 建立選站 Quick Reply ──────────────────────────────────────────
def make_station_quick_reply(stations, prefix=""):
    items = [
        QuickReplyItem(action=MessageAction(label=s, text=f"{prefix}{s}"))
        for s in stations
    ]
    return QuickReply(items=items)

# ── 解析日期 ──────────────────────────────────────────────────────
def parse_date(text):
    today = datetime.now()
    if "明天" in text:
        d = today + timedelta(days=1)
        return d.strftime("%Y-%m-%d"), "明天"
    elif "後天" in text:
        d = today + timedelta(days=2)
        return d.strftime("%Y-%m-%d"), "後天"
    else:
        return today.strftime("%Y-%m-%d"), "今天"

# ── LINE Webhook ──────────────────────────────────────────────────
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        # ── 打字輸入：「台北 高雄」或「台北 高雄 明天」
        parts = text.split()
        if len(parts) >= 2 and parts[0] in POPULAR_STATIONS and parts[1] in POPULAR_STATIONS:
            origin = parts[0]
            destination = parts[1]
            date_str, date_label = parse_date(text)
            trains = get_timetable(origin, destination, date_str)
            msg = format_result(trains, origin, destination, date_label)
            user_state.pop(user_id, None)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=msg)]
                )
            )
            return

        # ── 第二步：已選出發站，現在選目的站
        if user_id in user_state and user_state[user_id].get("step") == "choose_dest":
            origin = user_state[user_id]["origin"]
            # 過濾掉出發站
            dest_stations = [s for s in POPULAR_STATIONS if s != origin]
            if text in dest_stations:
                destination = text
                date_str, date_label = parse_date("")
                trains = get_timetable(origin, destination, date_str)
                msg = format_result(trains, origin, destination, date_label)
                user_state.pop(user_id, None)
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=msg)]
                    )
                )
                return
            else:
                # 不在清單，當作打字輸入目的站
                destination = text
                date_str, date_label = parse_date("")
                trains = get_timetable(origin, destination, date_str)
                msg = format_result(trains, origin, destination, date_label)
                user_state.pop(user_id, None)
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=msg)]
                    )
                )
                return

        # ── 第一步：選出發站（按鈕點進來的站名）
        if text in POPULAR_STATIONS:
            user_state[user_id] = {"step": "choose_dest", "origin": text}
            dest_stations = [s for s in POPULAR_STATIONS if s != text]
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(
                        text=f"出發站：{text}\n請選擇到達站（或直接打站名）",
                        quick_reply=make_station_quick_reply(dest_stations)
                    )]
                )
            )
            return

        # ── 預設：顯示選站選單
        user_state[user_id] = {"step": "choose_origin"}
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(
                    text="🚂 湯馬士小火車\n\n請選擇出發站（或直接打「台北 高雄」查詢）",
                    quick_reply=make_station_quick_reply(POPULAR_STATIONS)
                )]
            )
        )

@app.route("/", methods=["GET"])
def index():
    return "湯馬士小火車 Bot 運行中 🚂"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
