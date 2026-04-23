import os
import logging
from datetime import datetime, date, timedelta
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from flask import Flask, request
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.base import JobLookupError
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

KST = pytz.timezone("Asia/Seoul")

# ========================
# 설정
# ========================
CONFIG = {
    "MEMBER_A": os.environ.get("MEMBER_A", ""),
    "MEMBER_B": os.environ.get("MEMBER_B", ""),
    "MEMBER_C": os.environ.get("MEMBER_C", ""),
    "SHARE_CHANNEL": os.environ.get("SHARE_CHANNEL", ""),
    "DAILY_CHANNEL": os.environ.get("DAILY_CHANNEL", ""),
    "STANDUP_CHANNEL": os.environ.get("STANDUP_CHANNEL", ""),
    "SUPABASE_URL": os.environ.get("SUPABASE_URL", ""),
    "SUPABASE_SECRET_KEY": os.environ.get("SUPABASE_SECRET_KEY", ""),
    "STANDUP_HOUR": 10,
    "STANDUP_MINUTE": 0,
    "DAILY_BOT_HOUR": 18,
    "DAILY_BOT_MINUTE": 30,
}

COUNTRIES = [
    ("kr", "🇰🇷 한국"),
    ("jp", "🇯🇵 일본"),
    ("global", "🌏 글로벌"),
    ("tw", "🇹🇼 대만/홍콩"),
]

WORK_OPTIONS_TODAY = [
    "🧑🏻‍💻 정규 근무 (출근)",
    "🏡 정규 근무 (재택)",
    "🌞 오전 반차",
    "🌝 오후 반차",
]

WORK_OPTIONS_TOMORROW = [
    "🧑🏻‍💻 정규 근무 (출근)",
    "🏡 정규 근무 (재택)",
    "🌞 오전 반차",
    "🌝 오후 반차",
    "🏝️ 휴가",
]

VACATION_DAYS = ["1일", "2일", "3일", "4일", "5일"]

# ========================
# 상태 저장소
# ========================
standup_sessions = {}   # { user_id: { q1, q2, q2_vacation_days, memo, channel, ts } }
daily_sessions = {}     # { user_id: { q1, q1_1, channel } }
skip_standup = {}       # { user_id: skip_until_date }
afternoon_standup = {}  # { user_id: next_date }  → 오전 반차로 인해 오후 3시 발송 예정
daily_representative = None  # 오늘 데일리 봇 담당자 (기본 MEMBER_A)
scheduler = None


# ========================
# 유틸
# ========================
def get_member_name(user_id):
    try:
        user_info = app.client.users_info(user=user_id)
        return user_info["user"]["real_name"] or user_info["user"]["name"]
    except Exception as e:
        logger.error(f"유저 정보 조회 실패 ({user_id}): {e}")
        return user_id


def next_weekday(d, days=1):
    """다음 평일 날짜 반환"""
    result = d + timedelta(days=days)
    while result.weekday() >= 5:
        result += timedelta(days=1)
    return result


def get_today():
    return datetime.now(KST).date()


def get_tomorrow():
    return next_weekday(get_today())


# ========================
# Supabase 연동
# ========================
def get_today_ban_data():
    if not CONFIG["SUPABASE_URL"] or not CONFIG["SUPABASE_SECRET_KEY"]:
        return {}
    try:
        today = datetime.now(KST).strftime("%Y-%m-%d")
        url = f"{CONFIG['SUPABASE_URL']}/rest/v1/ban_records"
        headers = {
            "apikey": CONFIG["SUPABASE_SECRET_KEY"],
            "Authorization": f"Bearer {CONFIG['SUPABASE_SECRET_KEY']}",
        }
        params = {"select": "*", "created_at": f"gte.{today}T00:00:00"}
        resp = httpx.get(url, headers=headers, params=params)
        records = resp.json()
        result = {}
        for code, _ in COUNTRIES:
            items = [r for r in records if r.get("country") == code]
            if items:
                result[code] = "\n".join([f"{r['email']} : {r['reason']}" for r in items])
        return result
    except Exception as e:
        logger.error(f"Supabase 조회 실패: {e}")
        return {}


# ========================
# Standup 블록 빌더
# ========================
def build_standup_blocks(include_q1=True):
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "안녕하세요😆"}},
        {"type": "divider"},
    ]

    if include_q1:
        q1_options = [{"text": {"type": "plain_text", "text": opt}, "value": opt} for opt in WORK_OPTIONS_TODAY]
        blocks += [
            {"type": "section", "text": {"type": "mrkdwn", "text": "*1. 오늘 근무 형태를 알려주세요!*"}},
            {
                "type": "actions",
                "block_id": "standup_q1_block",
                "elements": [{
                    "type": "static_select",
                    "placeholder": {"type": "plain_text", "text": "선택해주세요"},
                    "options": q1_options,
                    "action_id": "standup_q1_select"
                }]
            },
            {"type": "divider"},
        ]

    q2_options = [{"text": {"type": "plain_text", "text": opt}, "value": opt} for opt in WORK_OPTIONS_TOMORROW]
    q2_num = "1" if not include_q1 else "2"
    blocks += [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{q2_num}. 내일 근무 형태를 알려주세요!*"}},
        {
            "type": "actions",
            "block_id": "standup_q2_block",
            "elements": [{
                "type": "static_select",
                "placeholder": {"type": "plain_text", "text": "선택해주세요"},
                "options": q2_options,
                "action_id": "standup_q2_select"
            }]
        },
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*팀원들에게 공유할 사항이 있으면 알려주세요😉*\n_(선택사항)_"}},
        {
            "type": "actions",
            "block_id": "standup_memo_block",
            "elements": [{
                "type": "button",
                "text": {"type": "plain_text", "text": "✏️ 공유사항 입력"},
                "action_id": "standup_memo_btn",
                "style": "primary"
            }, {
                "type": "button",
                "text": {"type": "plain_text", "text": "없음"},
                "action_id": "standup_memo_skip"
            }]
        }
    ]
    return blocks


def build_vacation_modal():
    options = [{"text": {"type": "plain_text", "text": d}, "value": d} for d in VACATION_DAYS]
    return {
        "type": "modal",
        "callback_id": "standup_vacation_modal",
        "title": {"type": "plain_text", "text": "휴가 기간"},
        "submit": {"type": "plain_text", "text": "확인"},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "🏝️ *휴가 기간을 선택해주세요*"}},
            {
                "type": "input",
                "block_id": "vacation_days_block",
                "element": {
                    "type": "static_select",
                    "action_id": "vacation_days_select",
                    "placeholder": {"type": "plain_text", "text": "기간 선택"},
                    "options": options
                },
                "label": {"type": "plain_text", "text": "휴가 기간"}
            }
        ]
    }


def build_memo_modal():
    return {
        "type": "modal",
        "callback_id": "standup_memo_modal",
        "title": {"type": "plain_text", "text": "공유사항 입력"},
        "submit": {"type": "plain_text", "text": "제출"},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": [
            {
                "type": "input",
                "block_id": "memo_input_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "memo_input",
                    "placeholder": {"type": "plain_text", "text": "공유할 내용을 입력해주세요"},
                    "multiline": True
                },
                "label": {"type": "plain_text", "text": "공유사항"}
            }
        ]
    }


# ========================
# Standup 발송
# ========================
def send_standup(user_id, include_q1=True):
    today = get_today()
    # 스킵 체크
    if user_id in skip_standup and skip_standup[user_id] >= today:
        logger.info(f"Standup 스킵: {user_id} (휴가 중)")
        return
    try:
        standup_sessions[user_id] = {"include_q1": include_q1}
        app.client.chat_postMessage(
            channel=user_id,
            text="안녕하세요😆 Standup을 시작합니다!",
            blocks=build_standup_blocks(include_q1=include_q1)
        )
        logger.info(f"Standup 발송: {user_id}")
    except Exception as e:
        logger.error(f"Standup 발송 실패 ({user_id}): {e}")


def send_standup_all():
    """평일 오전 10시 전체 발송"""
    global daily_representative
    daily_representative = CONFIG["MEMBER_A"]  # 기본값 초기화
    for user_id in [CONFIG["MEMBER_A"], CONFIG["MEMBER_B"]]:
        if user_id:
            send_standup(user_id, include_q1=True)


def check_member_a_response():
    """17시 기준 MEMBER_A 미응답 시 MEMBER_B로 전환"""
    global daily_representative
    member_a = CONFIG["MEMBER_A"]
    session = standup_sessions.get(member_a, {})
    if not session.get("submitted"):
        logger.info("MEMBER_A 17시까지 미응답 → MEMBER_B로 대표자 전환")
        daily_representative = CONFIG["MEMBER_B"]
        app.client.chat_postMessage(
            channel=CONFIG["MEMBER_B"],
            text="⚠️ MEMBER_A가 오늘 Standup을 제출하지 않아 오늘 데일리 보고 담당자가 되셨습니다!"
        )


# ========================
# Standup 채널 공유
# ========================
def post_standup_to_channel(user_id):
    session = standup_sessions.get(user_id, {})
    name = get_member_name(user_id)
    today_str = get_today().strftime("%m. %d")

    lines = [f"📋 *{today_str} Standup - {name}*"]
    if session.get("q1"):
        lines.append(f"• 오늘 근무: {session['q1']}")
    if session.get("q2"):
        q2_text = session['q2']
        if session.get("q2_vacation_days"):
            q2_text += f" ({session['q2_vacation_days']})"
        lines.append(f"• 내일 근무: {q2_text}")
    if session.get("memo"):
        lines.append(f"• 공유사항: {session['memo']}")

    message = "\n".join(lines)
    try:
        app.client.chat_postMessage(
            channel=CONFIG["STANDUP_CHANNEL"],
            text=message,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": message}}]
        )
    except Exception as e:
        logger.error(f"Standup 채널 공유 실패: {e}")


# ========================
# Standup 제출 완료 처리
# ========================
def finalize_standup(user_id, client):
    global daily_representative
    session = standup_sessions.get(user_id, {})
    session["submitted"] = True
    channel_id = session.get("channel")
    message_ts = session.get("ts")

    q1 = session.get("q1", "-")
    q2 = session.get("q2", "-")
    q2_days = session.get("q2_vacation_days", "")
    memo = session.get("memo", "")

    # 완료 메시지
    summary = f"✅ *Standup 제출 완료!*\n"
    if session.get("include_q1"):
        summary += f"• 오늘 근무: {q1}\n"
    q2_display = f"{q2} ({q2_days})" if q2_days else q2
    summary += f"• 내일 근무: {q2_display}\n"
    if memo:
        summary += f"• 공유사항: {memo}\n"
    summary += "\n오늘 하루도 화이팅입니다😎"

    try:
        if channel_id and message_ts:
            client.chat_update(
                channel=channel_id, ts=message_ts,
                text="Standup 제출 완료!",
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": summary}}]
            )
    except Exception as e:
        logger.error(f"Standup 완료 메시지 업데이트 실패: {e}")

    # 채널 공유
    post_standup_to_channel(user_id)

    # 추가 로직 처리
    tomorrow = get_tomorrow()

    # MEMBER_A 오후 반차 → 대표자 전환
    if user_id == CONFIG["MEMBER_A"] and q1 == "🌝 오후 반차":
        daily_representative = CONFIG["MEMBER_B"]
        logger.info("MEMBER_A 오후 반차 → 대표자 MEMBER_B로 전환")

    # 내일 오전 반차 → 다음날 오후 3시 발송
    if q2 == "🌞 오전 반차":
        afternoon_standup[user_id] = tomorrow
        schedule_afternoon_standup(user_id, tomorrow)
        logger.info(f"{user_id} 내일 오전 반차 → 오후 3시 Standup 예약")

    # 내일 휴가 → 선택 기간 동안 스킵
    if q2 == "🏝️ 휴가" and q2_days:
        days_num = int(q2_days.replace("일", ""))
        skip_until = tomorrow + timedelta(days=days_num - 1)
        # 주말 건너뜀
        actual_skip = tomorrow
        for _ in range(days_num - 1):
            actual_skip = next_weekday(actual_skip)
        skip_standup[user_id] = actual_skip
        logger.info(f"{user_id} 휴가 {days_num}일 → {actual_skip}까지 Standup 스킵")

        # MEMBER_A 휴가 → 익일 대표자 MEMBER_B로 전환
        if user_id == CONFIG["MEMBER_A"]:
            daily_representative = CONFIG["MEMBER_B"]
            logger.info("MEMBER_A 내일 휴가 → 익일 대표자 MEMBER_B로 전환")

    # MEMBER_A 당일 휴가 (q1) → 대표자 전환
    if user_id == CONFIG["MEMBER_A"] and session.get("include_q1") and q1 == "🏝️ 휴가":
        daily_representative = CONFIG["MEMBER_B"]
        logger.info("MEMBER_A 당일 휴가 → 대표자 MEMBER_B로 전환")


def schedule_afternoon_standup(user_id, target_date):
    """특정 날짜 오후 3시에 Standup 발송 예약"""
    global scheduler
    job_id = f"afternoon_standup_{user_id}_{target_date}"
    try:
        scheduler.add_job(
            lambda: send_standup(user_id, include_q1=False),
            trigger="date",
            run_date=KST.localize(datetime.combine(target_date, datetime.min.time().replace(hour=15))),
            id=job_id,
            replace_existing=True
        )
        logger.info(f"오후 3시 Standup 예약: {user_id} / {target_date}")
    except Exception as e:
        logger.error(f"오후 3시 Standup 예약 실패: {e}")


# ========================
# Standup 액션 핸들러
# ========================
@app.action("standup_q1_select")
def handle_standup_q1(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    value = body["actions"][0]["selected_option"]["value"]
    if user_id not in standup_sessions:
        standup_sessions[user_id] = {}
    standup_sessions[user_id]["q1"] = value
    standup_sessions[user_id]["channel"] = body["channel"]["id"]
    standup_sessions[user_id]["ts"] = body["message"]["ts"]


@app.action("standup_q2_select")
def handle_standup_q2(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    value = body["actions"][0]["selected_option"]["value"]
    if user_id not in standup_sessions:
        standup_sessions[user_id] = {}
    standup_sessions[user_id]["q2"] = value
    standup_sessions[user_id]["channel"] = body["channel"]["id"]
    standup_sessions[user_id]["ts"] = body["message"]["ts"]

    # 휴가 선택 시 기간 모달 열기
    if value == "🏝️ 휴가":
        try:
            client.views_open(trigger_id=body["trigger_id"], view=build_vacation_modal())
        except Exception as e:
            logger.error(f"휴가 모달 열기 실패: {e}")


@app.view("standup_vacation_modal")
def handle_vacation_modal(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    days = body["view"]["state"]["values"]["vacation_days_block"]["vacation_days_select"]["selected_option"]["value"]
    if user_id not in standup_sessions:
        standup_sessions[user_id] = {}
    standup_sessions[user_id]["q2_vacation_days"] = days


@app.action("standup_memo_btn")
def handle_standup_memo_btn(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    if user_id not in standup_sessions:
        standup_sessions[user_id] = {}
    standup_sessions[user_id]["channel"] = body["channel"]["id"]
    standup_sessions[user_id]["ts"] = body["message"]["ts"]
    try:
        client.views_open(trigger_id=body["trigger_id"], view=build_memo_modal())
    except Exception as e:
        logger.error(f"공유사항 모달 열기 실패: {e}")


@app.action("standup_memo_skip")
def handle_standup_memo_skip(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    if user_id not in standup_sessions:
        standup_sessions[user_id] = {}
    standup_sessions[user_id]["channel"] = body["channel"]["id"]
    standup_sessions[user_id]["ts"] = body["message"]["ts"]
    standup_sessions[user_id]["memo"] = ""

    session = standup_sessions.get(user_id, {})
    if not session.get("include_q1") or session.get("q1"):
        if session.get("q2"):
            finalize_standup(user_id, client)
        else:
            app.client.chat_postMessage(
                channel=user_id, text="⚠️ 내일 근무 형태를 먼저 선택해주세요!"
            )
    else:
        app.client.chat_postMessage(
            channel=user_id, text="⚠️ 오늘/내일 근무 형태를 먼저 선택해주세요!"
        )


@app.view("standup_memo_modal")
def handle_standup_memo_modal(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    memo = body["view"]["state"]["values"]["memo_input_block"]["memo_input"]["value"]
    if user_id not in standup_sessions:
        standup_sessions[user_id] = {}
    standup_sessions[user_id]["memo"] = memo

    session = standup_sessions.get(user_id, {})
    if not session.get("include_q1") or session.get("q1"):
        if session.get("q2"):
            finalize_standup(user_id, client)
        else:
            app.client.chat_postMessage(
                channel=user_id, text="⚠️ 내일 근무 형태를 먼저 선택해주세요!"
            )
    else:
        app.client.chat_postMessage(
            channel=user_id, text="⚠️ 오늘/내일 근무 형태를 먼저 선택해주세요!"
        )


# ========================
# 데일리 봇
# ========================
def build_daily_bot_blocks():
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


def send_daily_bot(target_user_id=None):
    global daily_representative
    if not target_user_id:
        target_user_id = daily_representative or CONFIG["MEMBER_A"]
    try:
        app.client.chat_postMessage(
            channel=target_user_id,
            text="데일리 업무 보고를 작성해주세요!",
            blocks=build_daily_bot_blocks()
        )
        logger.info(f"데일리 봇 발송: {target_user_id}")
    except Exception as e:
        logger.error(f"데일리 봇 발송 실패: {e}")


def post_final_daily(user_id):
    session = daily_sessions.get(user_id, {})
    name = get_member_name(user_id)
    today_str = datetime.now(KST).strftime("%m. %d")
    ban_data = get_today_ban_data()

    lines = [f"📊 *{today_str} 데일리 업무 보고*", f"작성자: {name}", ""]
    lines.append(f"*🤔 버그/특이사항: {session.get('q1', '-')}*")
    if session.get("q1_1"):
        lines.append(f"└ {session['q1_1']}")
    lines.append("")
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
    except Exception as e:
        logger.error(f"데일리 보고 채널 게시 실패: {e}")


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
            channel=channel_id, ts=message_ts, text="제출 완료",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": "📋 *데일리 업무 보고를 작성해주세요!*\n오늘 업무 내용을 공유해주세요 😊"}},
                {"type": "divider"},
                {"type": "section", "text": {"type": "mrkdwn", "text": "*1. 오늘 발생한 버그/특이 사항이 있나요?🤔*\n✅ 없음"}},
            ]
        )
        client.chat_postMessage(
            channel=channel_id, text="오늘도 고생 많으셨습니다! 🎉",
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "오늘도 고생 많으셨습니다! 🎉"}}]
        )
    except Exception as e:
        logger.error(f"q1 없음 처리 실패: {e}")
    post_final_daily(user_id)


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
                channel=channel_id, ts=q1_ts, text="제출 완료",
                blocks=[
                    {"type": "section", "text": {"type": "mrkdwn", "text": "📋 *데일리 업무 보고를 작성해주세요!*\n오늘 업무 내용을 공유해주세요 😊"}},
                    {"type": "divider"},
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"*1. 오늘 발생한 버그/특이 사항이 있나요?🤔*\n✅ 있음\n└ {text}"}},
                ]
            )
        client.chat_postMessage(
            channel=channel_id, text="오늘도 고생 많으셨습니다! 🎉",
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "오늘도 고생 많으셨습니다! 🎉"}}]
        )
    except Exception as e:
        logger.error(f"q1_1 제출 처리 실패: {e}")
    post_final_daily(user_id)


# ========================
# 슬래시 커맨드
# ========================
@app.command("/daily-now")
def handle_daily_now(ack, body, client):
    ack()
    user_id = body["user_id"]
    if user_id not in [CONFIG["MEMBER_A"], CONFIG["MEMBER_B"]]:
        client.chat_postMessage(channel=user_id, text="⚠️ 대표자만 사용할 수 있는 커맨드입니다.")
        return
    send_daily_bot(user_id)


@app.command("/standup-now")
def handle_standup_now(ack, body, client):
    """Standup 즉시 수신 (테스트용)"""
    ack()
    user_id = body["user_id"]
    send_standup(user_id, include_q1=True)


@app.command("/send-survey")
def handle_send_survey(ack, respond):
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
    global scheduler
    scheduler = BackgroundScheduler(timezone=KST)

    # 평일 오전 10시 - Standup 발송
    scheduler.add_job(
        send_standup_all,
        trigger="cron",
        day_of_week="mon-fri",
        hour=CONFIG["STANDUP_HOUR"],
        minute=CONFIG["STANDUP_MINUTE"],
        id="standup_all"
    )

    # 평일 17시 - MEMBER_A 미응답 체크
    scheduler.add_job(
        check_member_a_response,
        trigger="cron",
        day_of_week="mon-fri",
        hour=17,
        minute=0,
        id="check_member_a"
    )

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
    logger.info("스케줄러 시작 완료")
    return scheduler


if __name__ == "__main__":
    start_scheduler()
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
