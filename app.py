import os
import re
import logging
from datetime import datetime
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

KST = pytz.timezone("Asia/Seoul")

# ========================
# 설정
# ========================
CONFIG = {
    "MEMBER_A": os.environ.get("MEMBER_A", ""),
    "MEMBER_B": os.environ.get("MEMBER_B", ""),
    "MEMBER_C": os.environ.get("MEMBER_C", ""),
    "DAILY_CHANNEL": os.environ.get("DAILY_CHANNEL", ""),
    "BUG_REPORT_CHANNEL_ID": os.environ.get("BUG_REPORT_CHANNEL_ID", ""),
    "JIRA_BASE_URL": os.environ.get("JIRA_BASE_URL", ""),
    "SUPABASE_URL": os.environ.get("SUPABASE_URL", ""),
    "SUPABASE_SECRET_KEY": os.environ.get("SUPABASE_SECRET_KEY", ""),
    "DAILY_BOT_HOUR": 20,
    "DAILY_BOT_MINUTE": 30,
}

COUNTRIES = [
    ("kr", "🇰🇷 한국"),
    ("jp", "🇯🇵 일본"),
    ("global", "🌏 글로벌"),
    ("tw", "🇹🇼 대만/홍콩"),
]

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


# ========================
# Supabase 연동 - 영구정지 유저
# ========================
def get_today_ban_data():
    if not CONFIG["SUPABASE_URL"] or not CONFIG["SUPABASE_SECRET_KEY"]:
        return {}
    try:
        url = f"{CONFIG['SUPABASE_URL']}/rest/v1/ban_records"
        headers = {
            "apikey": CONFIG["SUPABASE_SECRET_KEY"],
            "Authorization": f"Bearer {CONFIG['SUPABASE_SECRET_KEY']}",
        }
        # KST 오늘 00:00 → UTC 변환 (KST = UTC+9)
        today_kst_start = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)
        today_utc_start = today_kst_start.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {"select": "*", "created_at": f"gte.{today_utc_start}"}
        resp = httpx.get(url, headers=headers, params=params)
        records = resp.json()
        logger.info(f"Supabase 응답: {len(records)}건 / 상태: {resp.status_code}")
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
# 지라 버그 티켓 자동 취합 (#버그-리포트 채널)
# ========================
# "OOO created a Bug QA-628 제목..." 패턴 매칭
CREATED_PATTERN = re.compile(
    r"^(?P<reporter>.+?)\s+created a\s+(?P<issue_type>\S+)\s+"
    r"(?P<key>[A-Z][A-Z0-9]+-\d+)\s+(?P<title>.+)$",
    re.DOTALL,
)


def get_todays_bugs_from_slack():
    """당일 #버그-리포트 채널에 새로 생성된 Jira 티켓 리스트 반환"""
    if not CONFIG["BUG_REPORT_CHANNEL_ID"]:
        logger.warning("BUG_REPORT_CHANNEL_ID 미설정 - 버그 취합 스킵")
        return []

    now_kst = datetime.now(KST)
    midnight_kst = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    oldest_ts = midnight_kst.timestamp()

    bugs = {}
    cursor = None
    try:
        while True:
            kwargs = {
                "channel": CONFIG["BUG_REPORT_CHANNEL_ID"],
                "oldest": oldest_ts,
                "limit": 200,
            }
            if cursor:
                kwargs["cursor"] = cursor

            resp = app.client.conversations_history(**kwargs)
            messages = resp.get("messages", [])

            for msg in messages:
                text = msg.get("text", "")
                match = CREATED_PATTERN.match(text.strip())
                if not match:
                    continue  # 댓글/상태변경 알림 등은 스킵, 신규 생성만

                key = match.group("key")
                if key in bugs:
                    continue

                title = match.group("title").split("\n")[0].strip()
                bugs[key] = {
                    "key": key,
                    "title": title,
                    "reporter": match.group("reporter").strip(),
                }

            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
    except Exception as e:
        logger.error(f"버그 리포트 채널 조회 실패: {e}")
        return list(bugs.values())

    return sorted(bugs.values(), key=lambda b: b["key"])


def format_bugs_section(bugs):
    if not bugs:
        return "🐞 *오늘 발생한 버그 (0건)*\n오늘 신규 접수된 버그 티켓이 없습니다 🎉"

    lines = [f"🐞 *오늘 발생한 버그 ({len(bugs)}건)*"]
    for i, bug in enumerate(bugs, 1):
        if CONFIG["JIRA_BASE_URL"]:
            key_display = f"<{CONFIG['JIRA_BASE_URL']}/browse/{bug['key']}|{bug['key']}>"
        else:
            key_display = f"`{bug['key']}`"
        line = f"{i}. {key_display} {bug['title']} _(제보: {bug['reporter']})_"
        lines.append(line)

    return "\n".join(lines)


# ========================
# 데일리 리포트 - 완전 자동 게시
# ========================
def post_daily_report():
    today_str = datetime.now(KST).strftime("%m. %d")

    bug_section = format_bugs_section(get_todays_bugs_from_slack())
    ban_data = get_today_ban_data()
    has_ban = bool(ban_data)

    lines = [f"📊 *{today_str} 데일리 리포트*", ""]
    lines.append(bug_section)
    lines.append("")
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
        logger.info("데일리 리포트 자동 게시 완료")
    except Exception as e:
        logger.error(f"데일리 리포트 게시 실패: {e}")


# ========================
# 추가 영구정지 보고 (/add-ban-report)
# ========================
COUNTRY_OPTIONS = [
    {"text": {"type": "plain_text", "text": "🇰🇷 한국"}, "value": "kr"},
    {"text": {"type": "plain_text", "text": "🇯🇵 일본"}, "value": "jp"},
    {"text": {"type": "plain_text", "text": "🌏 글로벌"}, "value": "global"},
    {"text": {"type": "plain_text", "text": "🇹🇼 대만/홍콩"}, "value": "tw"},
]

REASON_OPTIONS = [
    {"text": {"type": "plain_text", "text": "3대 악성 행위"}, "value": "3대 악성 행위"},
    {"text": {"type": "plain_text", "text": "연락처 교환 요구"}, "value": "연락처 교환 요구"},
    {"text": {"type": "plain_text", "text": "스캠"}, "value": "스캠"},
    {"text": {"type": "plain_text", "text": "피드 규칙 위반"}, "value": "피드 규칙 위반"},
    {"text": {"type": "plain_text", "text": "기타"}, "value": "기타"},
]

COUNTRY_LABEL = {
    "kr": "🇰🇷 한국",
    "jp": "🇯🇵 일본",
    "global": "🌏 글로벌",
    "tw": "🇹🇼 대만/홍콩"
}


def build_add_ban_modal(user_id, count=1):
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "🚨 *추가 영구정지 유저 정보를 입력해주세요.*"}}
    ]
    for i in range(count):
        blocks += [
            {"type": "divider"},
            {
                "type": "input",
                "block_id": f"country_{i}_block",
                "element": {
                    "type": "static_select",
                    "action_id": f"country_{i}_select",
                    "placeholder": {"type": "plain_text", "text": "국가 선택"},
                    "options": COUNTRY_OPTIONS
                },
                "label": {"type": "plain_text", "text": f"#{i+1} 국가 *"}
            },
            {
                "type": "input",
                "block_id": f"email_{i}_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": f"email_{i}_input",
                    "placeholder": {"type": "plain_text", "text": "user@example.com"}
                },
                "label": {"type": "plain_text", "text": f"#{i+1} 이메일 *"}
            },
            {
                "type": "input",
                "block_id": f"reason_{i}_block",
                "element": {
                    "type": "static_select",
                    "action_id": f"reason_{i}_select",
                    "placeholder": {"type": "plain_text", "text": "정지 사유 선택"},
                    "options": REASON_OPTIONS
                },
                "label": {"type": "plain_text", "text": f"#{i+1} 정지 사유 *"}
            },
            {
                "type": "input",
                "block_id": f"other_{i}_block",
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": f"other_{i}_input",
                    "placeholder": {"type": "plain_text", "text": "기타 사유 직접 입력 (기타 선택 시)"}
                },
                "label": {"type": "plain_text", "text": f"#{i+1} 기타 사유"}
            },
        ]
    blocks += [
        {"type": "divider"},
        {
            "type": "actions",
            "block_id": "add_more_block",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "➕ 한 명 더 추가"},
                    "action_id": "add_ban_more",
                    "value": str(count)
                }
            ]
        }
    ]
    return {
        "type": "modal",
        "callback_id": "add_ban_report_modal",
        "title": {"type": "plain_text", "text": "영구정지 추가 공유"},
        "submit": {"type": "plain_text", "text": "채널에 공유"},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": blocks
    }


@app.action("add_ban_more")
def handle_add_ban_more(ack, body, client):
    ack()
    current_count = int(body["actions"][0]["value"])
    new_count = current_count + 1
    user_id = body["user"]["id"]
    try:
        client.views_update(
            view_id=body["view"]["id"],
            view=build_add_ban_modal(user_id, count=new_count)
        )
    except Exception as e:
        logger.error(f"add_ban_more 모달 업데이트 실패: {e}")


@app.view("add_ban_report_modal")
def handle_add_ban_report_submit(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    values = body["view"]["state"]["values"]
    name = get_member_name(user_id)
    today_str = datetime.now(KST).strftime("%m. %d")

    count = 0
    while f"country_{count}_block" in values:
        count += 1

    country_groups = {}
    for i in range(count):
        country = values.get(f"country_{i}_block", {}).get(f"country_{i}_select", {}).get("selected_option", {}).get("value", "")
        email = values.get(f"email_{i}_block", {}).get(f"email_{i}_input", {}).get("value", "")
        reason = values.get(f"reason_{i}_block", {}).get(f"reason_{i}_select", {}).get("selected_option", {}).get("value", "")
        other = values.get(f"other_{i}_block", {}).get(f"other_{i}_input", {}).get("value", "")
        if not country or not email or not reason:
            continue
        final_reason = f"기타: {other}" if reason == "기타" and other else reason
        if country not in country_groups:
            country_groups[country] = []
        country_groups[country].append(f"{email} : {final_reason}")

    if not country_groups:
        return

    lines = [f"🚨 *영구정지 유저 추가 공유 ({today_str})*", f"공유: {name}", ""]
    for code, label in COUNTRIES:
        if country_groups.get(code):
            lines.append(f"\n{COUNTRY_LABEL[code]}")
            lines += country_groups[code]

    message = "\n".join(lines)
    try:
        app.client.chat_postMessage(
            channel=CONFIG["DAILY_CHANNEL"],
            text=message,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": message}}]
        )
        logger.info(f"추가 영구정지 보고 게시 완료: {name}")
    except Exception as e:
        logger.error(f"추가 영구정지 보고 게시 실패: {e}")


# ========================
# 슬래시 커맨드
# ========================
def _is_authorized(user_id):
    members = [m for m in [CONFIG["MEMBER_A"], CONFIG["MEMBER_B"], CONFIG["MEMBER_C"]] if m]
    return user_id in members


@app.command("/daily-now")
def handle_daily_now(ack, body, client):
    """데일리 리포트 즉시 발송"""
    ack()
    user_id = body["user_id"]
    if not _is_authorized(user_id):
        client.chat_postMessage(channel=user_id, text="⚠️ 담당자만 사용할 수 있는 커맨드입니다.")
        return
    post_daily_report()
    client.chat_postMessage(channel=user_id, text="✅ 데일리 리포트가 채널에 게시되었습니다.")


@app.command("/add-ban-report")
def handle_add_ban_report(ack, body, client):
    """추가 영구정지 유저 채널 공유"""
    ack()
    user_id = body["user_id"]
    if not _is_authorized(user_id):
        client.chat_postMessage(channel=user_id, text="⚠️ 담당자만 사용할 수 있는 커맨드입니다.")
        return
    try:
        client.views_open(
            trigger_id=body["trigger_id"],
            view=build_add_ban_modal(user_id, count=1)
        )
    except Exception as e:
        logger.error(f"/add-ban-report 모달 열기 실패: {e}")


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

    # 평일 오후 8시 30분 - 데일리 리포트 완전 자동 게시 (버그 자동 취합 + 영구정지 유저)
    scheduler.add_job(
        post_daily_report,
        trigger="cron",
        day_of_week="mon-fri",
        hour=CONFIG["DAILY_BOT_HOUR"],
        minute=CONFIG["DAILY_BOT_MINUTE"],
        id="daily_report"
    )

    scheduler.start()
    logger.info("스케줄러 시작 완료")
    return scheduler


# gunicorn 및 직접 실행 모두에서 스케줄러 시작
start_scheduler()

if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
