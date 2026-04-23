import os
import logging
from datetime import datetime, date
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from flask import Flask, request
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = App(
    token=os.environ.get("SLACK_BOT_TOKEN"),
    signing_secret=os.environ.get("SLACK_SIGNING_SECRET")
)

flask_app = Flask(__name__)
handler = SlackRequestHandler(app)

# ========================
# 설정
# ========================
CONFIG = {
    "MEMBER_A": os.environ.get("MEMBER_A", ""),   # 주 대표자 겸 담당자
    "MEMBER_B": os.environ.get("MEMBER_B", ""),   # 백업 대표자 겸 담당자
    "MEMBER_C": os.environ.get("MEMBER_C", ""),   # 담당자만
    "SHARE_CHANNEL": os.environ.get("SHARE_CHANNEL", ""),
    "DAILY_CHANNEL": os.environ.get("DAILY_CHANNEL", ""),
    "SUPABASE_URL": os.environ.get("SUPABASE_URL", ""),
    "SUPABASE_SECRET_KEY": os.environ.get("SUPABASE_SECRET_KEY", ""),
    "DAILY_BOT_HOUR": 18,
    "DAILY_BOT_MINUTE": 30,
    "TIMEZONE": "Asia/Seoul"
}

COUNTRIES = [
    ("kr", "🇰🇷 한국"),
    ("jp", "🇯🇵 일본"),
    ("global", "🌏 글로벌"),
    ("tw", "🇹🇼 대만/홍콩"),
]

# 대표자 세션 저장
daily_sessions = {}


# ========================
# Supabase 연동
# ========================
def get_today_ban_data():
    """Supabase에서 오늘 영구정지 유저 데이터 조회"""
    if not CONFIG["SUPABASE_URL"] or not CONFIG["SUPABASE_SECRET_KEY"]:
        logger.warning("Supabase 설정이 없습니다.")
        return {}

    try:
        today = datetime.now(pytz.timezone(CONFIG["TIMEZONE"])).strftime("%Y-%m-%d")
        url = f"{CONFIG['SUPABASE_URL']}/rest/v1/ban_records"
        headers = {
            "apikey": CONFIG["SUPABASE_SECRET_KEY"],
            "Authorization": f"Bearer {CONFIG['SUPABASE_SECRET_KEY']}",
            "Content-Type": "application/json"
        }
        params = {
            "select": "*",
            "created_at": f"gte.{today}T00:00:00"
        }
        resp = httpx.get(url, headers=headers, params=params)
        records = resp.json()

        # 국가별로 그룹핑
        result = {}
        for code, _ in COUNTRIES:
            items = [r for r in records if r.get("country") == code]
            if items:
                result[code] = "\n".join([f"{r['email']} : {r['reason']}" for r in items])
        return result
    except Exception as e:
        logger.error(f"Supabase 조회 실패: {e}")
        return {}


def format_ban_data_for_channel(ban_data):
    """채널 게시용 영구정지 데이터 포맷"""
    if not ban_data:
        return "없음"
    lines = []
    for code, label in COUNTRIES:
        if ban_data.get(code):
            lines.append(f"\n{label}")
            lines.append(ban_data[code])
    return "\n".join(lines) if lines else "없음"


# ========================
# 유저 이름 조회
# ========================
def get_member_name(user_id):
    try:
        user_info = app.client.users_info(user=user_id)
        return user_info["user"]["real_name"] or user_info["user"]["name"]
    except Exception as e:
        logger.error(f"유저 정보 조회 실패 ({user_id}): {e}")
        return user_id


def get_all_members():
    return [m for m in [CONFIG["MEMBER_A"], CONFIG["MEMBER_B"], CONFIG["MEMBER_C"]] if m]


# ========================
# 블록 빌더
# ========================
def build_daily_bot_blocks():
    """대표자 데일리 봇 메시지"""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": "📋 *데일리 업무 보고를 작성해주세요!*\n오늘 업무 내용을 공유해주세요 😊"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*1. 오늘 발생한 버그/특이 사항이 있나요?🤔*"}},
        {
            "type": "actions",
            "block_id": "q1_block",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "있음"}, "value": "있음", "action_id": "q1_있음", "style": "primary"},
                {"type": "button", "text": {"type": "plain_text", "text": "없음"}, "value": "없음", "action_id": "q1_없음"}
            ]
        }
    ]


def build_q1_1_modal():
    return {
        "type": "modal",
        "callback_id": "q1_1_modal",
        "title": {"type": "plain_text", "text": "버그/특이사항"},
        "submit": {"type": "plain_text", "text": "제출"},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "✏️ *상세 내용을 공유해주세요.*"}},
            {
                "type": "input",
                "block_id": "q1_1_input_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "q1_1_input",
                    "placeholder": {"type": "plain_text", "text": "버그 또는 특이사항 내용을 입력해주세요"},
                    "multiline": True
                },
                "label": {"type": "plain_text", "text": "상세 내용"}
            }
        ]
    }


def build_outro_block():
    return [{"type": "section", "text": {"type": "mrkdwn", "text": "오늘도 고생 많으셨습니다! 🎉"}}]


# ========================
# 대표자 데일리 봇 발송
# ========================
def send_daily_bot(target_user_id=None):
    """대표자에게 데일리 봇 발송"""
    if not target_user_id:
        member_a = CONFIG["MEMBER_A"]
        member_b = CONFIG["MEMBER_B"]
        # A가 설정되어 있으면 A에게, 없으면 B에게
        target_user_id = member_a if member_a else member_b

    try:
        app.client.chat_postMessage(
            channel=target_user_id,
            text="데일리 업무 보고를 작성해주세요!",
            blocks=build_daily_bot_blocks()
        )
        logger.info(f"데일리 봇 발송 완료: {target_user_id}")
    except Exception as e:
        logger.error(f"데일리 봇 발송 실패: {e}")


# ========================
# 최종 데일리 보고 채널 게시
# ========================
def post_final_daily(user_id):
    """최종 데일리 보고를 채널에 게시 (영구정지 데이터 Supabase에서 자동 조회)"""
    session = daily_sessions.get(user_id, {})
    name = get_member_name(user_id)
    today_str = datetime.now(pytz.timezone(CONFIG["TIMEZONE"])).strftime("%m. %d")

    # Supabase에서 오늘 영구정지 데이터 조회
    ban_data = get_today_ban_data()
    ban_text = format_ban_data_for_channel(ban_data)

    lines = [f"📊 *{today_str} 데일리 업무 보고*", f"작성자: {name}", ""]

    # 버그/특이사항
    lines.append(f"*🤔 버그/특이사항: {session.get('q1', '-')}*")
    if session.get("q1_1"):
        lines.append(f"└ {session['q1_1']}")

    lines.append("")

    # 영구정지 유저 (Supabase 자동 조회)
    has_ban = bool(ban_data)
    lines.append(f"*👿 영구정지 유저: {'있음' if has_ban else '없음'}*")
    if has_ban:
        for code, label in COUNTRIES:
            if ban_data.get(code):
                lines.append(f"\n{label}")
                lines.append(ban_data[code])

    message = "\n".join(lines)
    try:
        app.client.chat_postMessage(
            channel=CONFIG["DAILY_CHANNEL"],
            text=message,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": message}}]
        )
        logger.info(f"데일리 보고 채널 게시 완료: {name}")
    except Exception as e:
        logger.error(f"데일리 보고 채널 게시 실패: {e}")


# ========================
# 대표자 봇 - 버그/특이사항 있음
# ========================
@app.action("q1_있음")
def handle_q1_yes(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    if user_id not in daily_sessions:
        daily_sessions[user_id] = {}
    daily_sessions[user_id]["q1"] = "있음"
    daily_sessions[user_id]["channel"] = body["channel"]["id"]
    daily_sessions[user_id]["q1_ts"] = body["message"]["ts"]
    try:
        client.views_open(trigger_id=body["trigger_id"], view=build_q1_1_modal())
    except Exception as e:
        logger.error(f"q1_1 모달 열기 실패: {e}")


# ========================
# 대표자 봇 - 버그/특이사항 없음
# ========================
@app.action("q1_없음")
def handle_q1_no(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    channel_id = body["channel"]["id"]
    message_ts = body["message"]["ts"]
    if user_id not in daily_sessions:
        daily_sessions[user_id] = {}
    daily_sessions[user_id]["q1"] = "없음"
    daily_sessions[user_id]["channel"] = channel_id

    try:
        client.chat_update(
            channel=channel_id, ts=message_ts,
            text="제출 완료",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": "📋 *데일리 업무 보고를 작성해주세요!*\n오늘 업무 내용을 공유해주세요 😊"}},
                {"type": "divider"},
                {"type": "section", "text": {"type": "mrkdwn", "text": "*1. 오늘 발생한 버그/특이 사항이 있나요?🤔*\n✅ 없음"}},
            ]
        )
        client.chat_postMessage(
            channel=channel_id,
            text="오늘도 고생 많으셨습니다! 🎉",
            blocks=build_outro_block()
        )
    except Exception as e:
        logger.error(f"q1 없음 처리 실패: {e}")

    post_final_daily(user_id)


# ========================
# 버그/특이사항 모달 제출
# ========================
@app.view("q1_1_modal")
def handle_q1_1_submit(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    text = body["view"]["state"]["values"]["q1_1_input_block"]["q1_1_input"]["value"]
    if user_id not in daily_sessions:
        daily_sessions[user_id] = {}
    daily_sessions[user_id]["q1_1"] = text
    channel_id = daily_sessions[user_id].get("channel")
    q1_ts = daily_sessions[user_id].get("q1_ts")

    try:
        if channel_id and q1_ts:
            client.chat_update(
                channel=channel_id, ts=q1_ts,
                text="제출 완료",
                blocks=[
                    {"type": "section", "text": {"type": "mrkdwn", "text": "📋 *데일리 업무 보고를 작성해주세요!*\n오늘 업무 내용을 공유해주세요 😊"}},
                    {"type": "divider"},
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"*1. 오늘 발생한 버그/특이 사항이 있나요?🤔*\n✅ 있음\n└ {text}"}},
                ]
            )
        client.chat_postMessage(
            channel=channel_id,
            text="오늘도 고생 많으셨습니다! 🎉",
            blocks=build_outro_block()
        )
    except Exception as e:
        logger.error(f"q1_1 제출 처리 실패: {e}")

    post_final_daily(user_id)


# ========================
# 슬래시 커맨드
# ========================
@app.command("/daily-now")
def handle_daily_now(ack, body, client):
    """대표자가 데일리 봇을 즉시 수신하는 커맨드"""
    ack()
    user_id = body["user_id"]
    rep_a = CONFIG["MEMBER_A"]
    rep_b = CONFIG["MEMBER_B"]
    if user_id not in [rep_a, rep_b]:
        client.chat_postMessage(channel=user_id, text="⚠️ 대표자만 사용할 수 있는 커맨드입니다.")
        return
    send_daily_bot(user_id)


@app.command("/send-survey")
def handle_send_survey(ack, body, respond):
    """데일리 봇 수동 발송 (관리자용)"""
    ack()
    send_daily_bot()
    respond("✅ 데일리 봇이 발송되었습니다!")


# ========================
# Flask 라우트
# ========================
@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)


@flask_app.route("/slack/actions", methods=["POST"])
def slack_actions():
    return handler.handle(request)


@flask_app.route("/slack/commands", methods=["POST"])
def slack_commands():
    return handler.handle(request)


@flask_app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "time": str(datetime.now())}


# ========================
# 스케줄러
# ========================
def start_scheduler():
    tz = pytz.timezone(CONFIG["TIMEZONE"])
    scheduler = BackgroundScheduler(timezone=tz)

    # 평일 오후 6시 30분 - 대표자 데일리 봇 발송
    scheduler.add_job(
        send_daily_bot,
        trigger="cron",
        day_of_week="mon-fri",
        hour=CONFIG["DAILY_BOT_HOUR"],
        minute=CONFIG["DAILY_BOT_MINUTE"],
        id="daily_bot"
    )

    scheduler.start()
    logger.info(f"스케줄러 시작: 평일 {CONFIG['DAILY_BOT_HOUR']:02d}:{CONFIG['DAILY_BOT_MINUTE']:02d} 데일리 봇 발송")
    return scheduler


if __name__ == "__main__":
    start_scheduler()
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
