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

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
TDX_CLIENT_ID = os.environ.get("TDX_CLIENT_ID")
TDX_CLIENT_SECRET = os.environ.get("TDX_CLIENT_SECRET")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 站名 → TDX 站代碼
STATION_MAP = {
    "台北": "1000",
    "板橋": "1010",
    "桃園": "1040",
    "新竹": "1060",
    "台中": "1120",
    "嘉義": "1190",
    "台南": "1210",
    "高雄": "1225",
    "花蓮": "0500",
    "台東": "0360",
}

POPULAR_STATIONS = list(STATION_MAP.keys())
user_state = {}

def get_tdx_token():
    url = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": TDX_CLIENT_ID,
        "client_secret": TDX_CLIENT_SECRET,
    }
    res = requests.post(url, data=data)
    return res.json().get("access_token")

def get_timetable(origin_name, destination_name, date_str):
    origin_id = STATION_MAP.get(origin_name)
    dest_id = STATION_MAP.get(destination_name)
    if not origin_id or not dest_id:
        return None

    token = get_tdx_token()
    headers = {"Authorization": f"Bearer {token}"}
    url = (
        f"https://tdx.transportdata.tw/api/basic/v3/Rail/TRA/DailyTrainTimetable/OD"
        f"/{origin_id}/{dest_id}/{date_str}"
        f"?%24top=5&%24format=JSON"
    )
    res = requests.get(url, headers=headers)
    if res.status_code != 200:
        return None
    data = res.json()
    return data.get("TrainTimetables", [])[:5]

def format_result(trains, origin, destination, date_label):
    if not trains:
        return f"😕 找不到 {origin} → {destination} 的班次\n請確認站名是否正確"

    lines = [f"🚂 {origin} → {destination}｜{date_label}班次\n{'─' * 20}"]
    for t in trains:
        info = t.get("TrainInfo", {})
        stops = t.get("StopTimes", [])

        train_no = info.get("TrainNo", "")
        train_type = info.get("TrainTypeName", {}).get("Zh_tw", "")

        origin_id = STATION_MAP.get(origin)
        dest_id = STATION_MAP.get(destination)

        dep = next((s for s in stops if s.get("StationID") == origin_id), None)
        arr = next((s for s in stops if s.get("StationID") == dest_id), None)

        if not dep or not arr:
            continue

        dep_time = dep.get("DepartureTime", "--:--")
        arr_time = arr.get("ArrivalTime", "--:--")

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

    if len(lines) == 1:
        return f"😕 找不到 {origin} → {destination} 的班次"

    lines.append("\n查明天請傳：台北 高雄 明天")
    return "\n".join(lines)

def make_station_quick_reply(stations):
    items = [
        QuickReplyItem(action=MessageAction(label=s, text=s))
        for s in stations
    ]
    return QuickReply(items=items)

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

        # 打字輸入：「台北 高雄」或「台北 高雄 明天」
        parts = text.split()
        if len(parts) >= 2 and parts[0] in STATION_MAP and parts[1] in STATION_MAP:
            origin, destination = parts[0], parts[1]
            date_str, date_label = parse_date(text)
            trains = get_timetable(origin, destination, date_str)
            msg = format_result(trains, origin, destination, date_label)
            user_state.pop(user_id, None)
            line_bot_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=msg)]
            ))
            return

        # 第二步：選目的站
        if user_id in user_state and user_state[user_id].get("step") == "choose_dest":
            origin = user_state[user_id]["origin"]
            destination = text if text in STATION_MAP else text
            if destination not in STATION_MAP:
                line_bot_api.reply_message(ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=f"😕 找不到「{destination}」這個站，請選按鈕或輸入正確站名")]
                ))
                return
            date_str, date_label = parse_date("")
            trains = get_timetable(origin, destination, date_str)
            msg = format_result(trains, origin, destination, date_label)
            user_state.pop(user_id, None)
            line_bot_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=msg)]
            ))
            return

        # 第一步：選出發站
        if text in STATION_MAP:
            user_state[user_id] = {"step": "choose_dest", "origin": text}
            dest_stations = [s for s in POPULAR_STATIONS if s != text]
            line_bot_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(
                    text=f"出發站：{text}\n請選擇到達站（或直接打站名）",
                    quick_reply=make_station_quick_reply(dest_stations)
                )]
            ))
            return

        # 預設：顯示選站選單
        user_state[user_id] = {"step": "choose_origin"}
        line_bot_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(
                text="🚂 湯馬士小火車\n\n請選擇出發站（或直接打「台北 高雄」查詢）",
                quick_reply=make_station_quick_reply(POPULAR_STATIONS)
            )]
        ))

@app.route("/", methods=["GET"])
def index():
    return "湯馬士小火車 Bot 運行中 🚂"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
