import os
import logging
from datetime import datetime
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from flask import Flask, request
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = App(
    token=os.environ.get("SLACK_BOT_TOKEN"),
    signing_secret=os.environ.get("SLACK_SIGNING_SECRET")
)

flask_app = Flask(__name__)
handler = SlackRequestHandler(app)

SURVEY_CONFIG = {
    "target_members": [
        "U039KB8CF3J",
    ],
    "result_channel": os.environ.get("RESULT_CHANNEL_ID", "C0B04E3HY0Y"),
    "send_hour": 11,
    "send_minute": 15,
    "timezone": "Asia/Seoul"
}

user_sessions = {}


def build_intro_block():
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "오늘 업무 내용을 공유해주세요😆"}
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*1. 오늘 발생한 버그/특이 사항이 있나요?🤔*"}
        },
        {
            "type": "actions",
            "block_id": "q1_block",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "있음"},
                    "value": "있음",
                    "action_id": "q1_있음",
                    "style": "primary"
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "없음"},
                    "value": "없음",
                    "action_id": "q1_없음"
                }
            ]
        }
    ]


def build_q2_block():
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*2. 오늘 영구정지 유저가 있었나요?👿*"}
        },
        {
            "type": "actions",
            "block_id": "q2_block",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "있음"},
                    "value": "있음",
                    "action_id": "q2_있음",
                    "style": "danger"
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "없음"},
                    "value": "없음",
                    "action_id": "q2_없음"
                }
            ]
        }
    ]


def build_outro_block():
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "오늘도 고생 많으셨습니다! 🎉"}
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
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "✏️ *상세 내용을 공유해주세요.*"}
            },
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


def build_q2_1_modal():
    return {
        "type": "modal",
        "callback_id": "q2_1_modal",
        "title": {"type": "plain_text", "text": "영구정지 유저"},
        "submit": {"type": "plain_text", "text": "제출"},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "🚨 *영구정지 유저 정보를 공유해주세요.*"}
            },
            {
                "type": "input",
                "block_id": "q2_1_input_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "q2_1_input",
                    "placeholder": {"type": "plain_text", "text": "유저 정보를 입력해주세요"},
                    "multiline": True
                },
                "label": {"type": "plain_text", "text": "유저 정보"}
            }
        ]
    }


def send_survey_to_members():
    logger.info(f"[{datetime.now()}] 설문 발송 시작")
    for user_id in SURVEY_CONFIG["target_members"]:
        try:
            user_sessions[user_id] = {}
            app.client.chat_postMessage(
                channel=user_id,
                text="오늘 업무 내용을 공유해주세요😆",
                blocks=build_intro_block()
            )
            logger.info(f"설문 발송 완료: {user_id}")
        except Exception as e:
            logger.error(f"설문 발송 실패 ({user_id}): {e}")


def post_final_result(user_id):
    session = user_sessions.get(user_id, {})
    try:
        user_info = app.client.users_info(user=user_id)
        user_name = user_info["user"]["real_name"] or user_info["user"]["name"]

        lines = [f"📋 *{user_name}*님의 업무 보고"]
        lines.append(f"• 버그/특이사항: *{session.get('q1', '-')}*")
        if session.get("q1_1"):
            lines.append(f"  └ 상세내용: {session['q1_1']}")
        lines.append(f"• 영구정지 유저: *{session.get('q2', '-')}*")
        if session.get("q2_1"):
            lines.append(f"  └ 유저정보: {session['q2_1']}")

        message = "\n".join(lines)
        app.client.chat_postMessage(
            channel=SURVEY_CONFIG["result_channel"],
            text=message,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": message}}]
        )
        logger.info(f"결과 채널 게시 완료: {user_name}")
    except Exception as e:
        logger.error(f"결과 채널 게시 실패: {e}")


@app.action("q1_있음")
def handle_q1_yes(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    if user_id not in user_sessions:
        user_sessions[user_id] = {}
    user_sessions[user_id]["q1"] = "있음"
    user_sessions[user_id]["q1_channel"] = body["channel"]["id"]
    user_sessions[user_id]["q1_ts"] = body["message"]["ts"]
    try:
        client.views_open(trigger_id=body["trigger_id"], view=build_q1_1_modal())
    except Exception as e:
        logger.error(f"q1_1 모달 열기 실패: {e}")


@app.action("q1_없음")
def handle_q1_no(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    if user_id not in user_sessions:
        user_sessions[user_id] = {}
    user_sessions[user_id]["q1"] = "없음"
    channel_id = body["channel"]["id"]
    message_ts = body["message"]["ts"]
    try:
        client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text="질문 1 완료",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": "오늘 업무 내용을 공유해주세요😆"}},
                {"type": "divider"},
                {"type": "section", "text": {"type": "mrkdwn", "text": "*1. 오늘 발생한 버그/특이 사항이 있나요?🤔*\n✅ 없음"}},
            ]
        )
        client.chat_postMessage(channel=channel_id, text="질문 2", blocks=build_q2_block())
    except Exception as e:
        logger.error(f"q1 없음 처리 실패: {e}")


@app.view("q1_1_modal")
def handle_q1_1_submit(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    text = body["view"]["state"]["values"]["q1_1_input_block"]["q1_1_input"]["value"]
    user_sessions[user_id]["q1_1"] = text
    channel_id = user_sessions[user_id].get("q1_channel")
    message_ts = user_sessions[user_id].get("q1_ts")
    try:
        if channel_id and message_ts:
            client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text="질문 1 완료",
                blocks=[
                    {"type": "section", "text": {"type": "mrkdwn", "text": "오늘 업무 내용을 공유해주세요😆"}},
                    {"type": "divider"},
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"*1. 오늘 발생한 버그/특이 사항이 있나요?🤔*\n✅ 있음\n└ {text}"}},
                ]
            )
        client.chat_postMessage(channel=channel_id, text="질문 2", blocks=build_q2_block())
    except Exception as e:
        logger.error(f"q1_1 제출 처리 실패: {e}")


@app.action("q2_있음")
def handle_q2_yes(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    if user_id not in user_sessions:
        user_sessions[user_id] = {}
    user_sessions[user_id]["q2"] = "있음"
    user_sessions[user_id]["q2_channel"] = body["channel"]["id"]
    user_sessions[user_id]["q2_ts"] = body["message"]["ts"]
    try:
        client.views_open(trigger_id=body["trigger_id"], view=build_q2_1_modal())
    except Exception as e:
        logger.error(f"q2_1 모달 열기 실패: {e}")


@app.action("q2_없음")
def handle_q2_no(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    if user_id not in user_sessions:
        user_sessions[user_id] = {}
    user_sessions[user_id]["q2"] = "없음"
    channel_id = body["channel"]["id"]
    message_ts = body["message"]["ts"]
    try:
        client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text="질문 2 완료",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": "*2. 오늘 영구정지 유저가 있었나요?👿*\n✅ 없음"}},
            ]
        )
        client.chat_postMessage(channel=channel_id, text="오늘도 고생 많으셨습니다! 🎉", blocks=build_outro_block())
    except Exception as e:
        logger.error(f"q2 없음 처리 실패: {e}")
    post_final_result(user_id)


@app.view("q2_1_modal")
def handle_q2_1_submit(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    text = body["view"]["state"]["values"]["q2_1_input_block"]["q2_1_input"]["value"]
    user_sessions[user_id]["q2_1"] = text
    channel_id = user_sessions[user_id].get("q2_channel")
    message_ts = user_sessions[user_id].get("q2_ts")
    try:
        if channel_id and message_ts:
            client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text="질문 2 완료",
                blocks=[
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"*2. 오늘 영구정지 유저가 있었나요?👿*\n✅ 있음\n└ {text}"}},
                ]
            )
        client.chat_postMessage(channel=channel_id, text="오늘도 고생 많으셨습니다! 🎉", blocks=build_outro_block())
    except Exception as e:
        logger.error(f"q2_1 제출 처리 실패: {e}")
    post_final_result(user_id)


@app.command("/send-survey")
def handle_send_survey(ack, respond):
    ack()
    send_survey_to_members()
    respond("✅ 설문이 발송되었습니다!")


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


def start_scheduler():
    tz = pytz.timezone(SURVEY_CONFIG["timezone"])
    scheduler = BackgroundScheduler(timezone=tz)
    scheduler.add_job(
        send_survey_to_members,
        trigger="cron",
        hour=SURVEY_CONFIG["send_hour"],
        minute=SURVEY_CONFIG["send_minute"],
        id="daily_survey"
    )
    scheduler.start()
    logger.info(f"스케줄러 시작: 매일 {SURVEY_CONFIG['send_hour']:02d}:{SURVEY_CONFIG['send_minute']:02d} ({SURVEY_CONFIG['timezone']}) 발송")
    return scheduler


if __name__ == "__main__":
    scheduler = start_scheduler()
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
