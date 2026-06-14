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

POPULAR_STATIONS = ["台北", "板橋", "桃園", "新竹", "台中", "嘉義", "台南", "高雄", "花蓮", "台東"]

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

def get_station_id(station_name, token):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://tdx.transportdata.tw/api/basic/v3/Rail/TRA/Station?$filter=StationName/Zh_tw eq '{station_name}'&$format=JSON"
    res = requests.get(url, headers=headers)
    if res.status_code != 200:
        return None
    stations = res.json().get("Stations", [])
    if stations:
        return stations[0].get("StationID")
    return None

def get_timetable(origin_name, destination_name, date_str):
    token = get_tdx_token()
    
    origin_id = get_station_id(origin_name, token)
    dest_id = get_station_id(destination_name, token)
    
    if not origin_id or not dest_id:
        return None, f"找不到站名：{'、'.join([s for s, i in [(origin_name, origin_id), (destination_name, dest_id)] if not i])}"

    headers = {"Authorization": f"Bearer {token}"}
    url = (
        f"https://tdx.transportdata.tw/api/basic/v3/Rail/TRA/DailyTrainTimetable/OD"
        f"/{origin_id}/{dest_id}/{date_str}"
        f"?%24top=5&%24format=JSON"
    )
    res = requests.get(url, headers=headers)
    if res.status_code != 200:
        return None, f"API 錯誤：{res.status_code}"
    
    data = res.json()
    trains = data.get("TrainTimetables", [])
    return trains[:5], None

def format_result(trains, origin, destination, date_label, error=None):
    if error:
        return f"😕 查詢失敗：{error}"
    if not trains:
        return f"😕 找不到 {origin} → {destination} 的班次\n可能今天沒有直達車，或站名有誤"

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

        parts = text.split()
        if len(parts) >= 2 and parts[0] in POPULAR_STATIONS and parts[1] in POPULAR_STATIONS:
            origin, destination = parts[0], parts[1]
            date_str, date_label = parse_date(text)
            trains, error = get_timetable(origin, destination, date_str)
            msg = format_result(trains, origin, destination, date_label, error)
            user_state.pop(user_id, None)
            line_bot_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=msg)]
            ))
            return

        if user_id in user_state and user_state[user_id].get("step") == "choose_dest":
            origin = user_state[user_id]["origin"]
            destination = text
            date_str, date_label = parse_date("")
            trains, error = get_timetable(origin, destination, date_str)
            msg = format_result(trains, origin, destination, date_label, error)
            user_state.pop(user_id, None)
            line_bot_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=msg)]
            ))
            return

        if text in POPULAR_STATIONS:
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
