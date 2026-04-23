import os
import json
import logging
from datetime import datetime
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from flask import Flask, request
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Slack App 초기화
app = App(
    token=os.environ.get("SLACK_BOT_TOKEN"),
    signing_secret=os.environ.get("SLACK_SIGNING_SECRET")
)

flask_app = Flask(__name__)
handler = SlackRequestHandler(app)

# ========================
# 설문 설정 (여기서 커스터마이즈)
# ========================
SURVEY_CONFIG = {
    "question": "오늘 업무 내용을 공유해주세요 😊",
    "options": [
        "기획/전략 업무",
        "개발/기술 업무",
        "디자인 업무",
        "미팅/커뮤니케이션",
        "기타"
    ],
    "other_option": "기타",
    "target_members": [
        "U039KB8CF3J",  # 따옴표와 쉼표 추가!
    ],
    "result_channel": os.environ.get("RESULT_CHANNEL_ID", "C0B04E3HY0Y"),
    "send_hour": 11,
    "send_minute": 15,
    "timezone": "Asia/Seoul"
}

# 진행 중인 설문 응답 임시 저장 (기타 입력 대기 상태)
pending_other_input = {}


def build_survey_blocks():
    """설문 메시지 블록 생성"""
    options = []
    for opt in SURVEY_CONFIG["options"]:
        options.append({
            "text": {"type": "plain_text", "text": opt},
            "value": opt
        })

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{SURVEY_CONFIG['question']}*"
            }
        },
        {
            "type": "actions",
            "block_id": "survey_options",
            "elements": [
                {
                    "type": "static_select",
                    "placeholder": {"type": "plain_text", "text": "선택해주세요"},
                    "options": options,
                    "action_id": "survey_select"
                }
            ]
        }
    ]


def send_survey_to_members():
    """지정된 멤버들에게 DM으로 설문 발송"""
    logger.info(f"[{datetime.now()}] 설문 발송 시작")

    if not SURVEY_CONFIG["target_members"]:
        logger.warning("target_members가 설정되지 않았습니다. app.py의 SURVEY_CONFIG를 확인해주세요.")
        return

    for user_id in SURVEY_CONFIG["target_members"]:
        try:
            app.client.chat_postMessage(
                channel=user_id,
                text=SURVEY_CONFIG["question"],
                blocks=build_survey_blocks()
            )
            logger.info(f"설문 발송 완료: {user_id}")
        except Exception as e:
            logger.error(f"설문 발송 실패 ({user_id}): {e}")


def post_result_to_channel(user_id, answer, is_other=False, other_text=""):
    """답변을 결과 채널에 공개"""
    if not SURVEY_CONFIG["result_channel"]:
        logger.warning("result_channel이 설정되지 않았습니다.")
        return

    try:
        # 유저 정보 가져오기
        user_info = app.client.users_info(user=user_id)
        user_name = user_info["user"]["real_name"] or user_info["user"]["name"]

        if is_other and other_text:
            message = f"*{user_name}*님의 답변:\n> {answer} → {other_text}"
        else:
            message = f"*{user_name}*님의 답변:\n> {answer}"

        app.client.chat_postMessage(
            channel=SURVEY_CONFIG["result_channel"],
            text=message,
            blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": message}
                }
            ]
        )
        logger.info(f"결과 채널 게시 완료: {user_name} → {answer}")
    except Exception as e:
        logger.error(f"결과 채널 게시 실패: {e}")


# ========================
# 객관식 선택 처리
# ========================
@app.action("survey_select")
def handle_survey_select(ack, body, client):
    ack()

    user_id = body["user"]["id"]
    selected_value = body["actions"][0]["selected_option"]["value"]
    channel_id = body["channel"]["id"]
    message_ts = body["message"]["ts"]

    # "기타" 선택 시 주관식 입력창(모달) 열기
    if selected_value == SURVEY_CONFIG["other_option"]:
        pending_other_input[user_id] = {
            "channel_id": channel_id,
            "message_ts": message_ts
        }
        try:
            client.views_open(
                trigger_id=body["trigger_id"],
                view={
                    "type": "modal",
                    "callback_id": "other_input_modal",
                    "title": {"type": "plain_text", "text": "직접 입력"},
                    "submit": {"type": "plain_text", "text": "제출"},
                    "close": {"type": "plain_text", "text": "취소"},
                    "blocks": [
                        {
                            "type": "input",
                            "block_id": "other_text_block",
                            "element": {
                                "type": "plain_text_input",
                                "action_id": "other_text_input",
                                "placeholder": {"type": "plain_text", "text": "업무 내용을 입력해주세요"},
                                "multiline": True
                            },
                            "label": {"type": "plain_text", "text": "기타 내용"}
                        }
                    ]
                }
            )
        except Exception as e:
            logger.error(f"모달 열기 실패: {e}")
    else:
        # 일반 선택지 → 즉시 처리
        try:
            # 원래 메시지를 완료 상태로 업데이트
            client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text=f"✅ 답변 완료: *{selected_value}*",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"✅ 답변이 제출되었습니다!\n선택: *{selected_value}*"
                        }
                    }
                ]
            )
        except Exception as e:
            logger.error(f"메시지 업데이트 실패: {e}")

        # 결과 채널에 공개
        post_result_to_channel(user_id, selected_value)


# ========================
# 기타 주관식 모달 제출 처리
# ========================
@app.view("other_input_modal")
def handle_other_input(ack, body, client):
    ack()

    user_id = body["user"]["id"]
    other_text = body["view"]["state"]["values"]["other_text_block"]["other_text_input"]["value"]

    # DM 메시지 업데이트
    if user_id in pending_other_input:
        info = pending_other_input.pop(user_id)
        try:
            client.chat_update(
                channel=info["channel_id"],
                ts=info["message_ts"],
                text=f"✅ 답변 완료: 기타 → {other_text}",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"✅ 답변이 제출되었습니다!\n선택: *기타*\n내용: {other_text}"
                        }
                    }
                ]
            )
        except Exception as e:
            logger.error(f"메시지 업데이트 실패: {e}")

    # 결과 채널에 공개
    post_result_to_channel(user_id, SURVEY_CONFIG["other_option"], is_other=True, other_text=other_text)


# ========================
# 수동 트리거 슬래시 커맨드 (/send-survey)
# ========================
@app.command("/send-survey")
def handle_send_survey(ack, respond):
    ack()
    send_survey_to_members()
    respond("✅ 설문이 발송되었습니다!")


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
# 스케줄러 (매일 자동 발송)
# ========================
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
