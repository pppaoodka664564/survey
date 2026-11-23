# -*- coding: utf-8 -*-
"""
校园生活与服务问卷调查 —— 后端应用

技术栈：Flask + SQLite（标准库 sqlite3）+ gunicorn
部署目标：Hugging Face Docker Space（端口 7860）

关键设计（校园场景）：
- 一个学校很多学生共用一个出口 IP，因此 IP 只记录、不去重、不封禁，
  IP 不是唯一键，同一 IP 可以提交任意多份问卷。
- 防刷只做两层：
  1. 蜜罐字段 honeypot（前端隐藏输入，填了就拒绝）；
  2. 宽松频率限制：每 IP 每 10 分钟最多 60 次提交（内存滑动窗口）。
  一个班同时交卷完全没问题。

数据备份：
- 每次写入（提交、删除）后，把 SQLite 文件上传到 Hugging Face 数据集仓库，
  Space 重启/重建后启动时自动从仓库下载恢复。
- 环境变量 HF_BACKUP_REPO / HF_TOKEN 未配置时，只用本地 SQLite 并打警告日志。

管理员：
- 密码取环境变量 ADMIN_PASSWORD；未配置时默认 admin123（仅供本地测试，
  生产环境必须配置）。
- 登录后用 Flask 签名 session cookie 保持登录态。
"""

import csv
import io
import json
import logging
import os
import secrets
import sqlite3
import threading
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone

from flask import (
    Flask,
    abort,
    g,
    jsonify,
    render_template,
    request,
    send_from_directory,
    session,
)
from werkzeug.middleware.proxy_fix import ProxyFix

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "survey.db")
HF_REPO_FILENAME = "survey.db"

ADMIN_USER = os.environ.get("ADMIN_USER") or "admin"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or "admin123"
SECRET_KEY = os.environ.get("SECRET_KEY")
HF_BACKUP_REPO = os.environ.get("HF_BACKUP_REPO", "").strip()
HF_TOKEN = os.environ.get("HF_TOKEN", "").strip()

# 频率限制：每 IP 每 10 分钟最多 60 次提交
RATE_LIMIT_WINDOW_SECONDS = 600
RATE_LIMIT_MAX = 60

# 文本答案长度上限（防滥用）
MAX_TEXT_LEN = 2000
MAX_OTHER_LEN = 500

# 前端选项白名单（与 static/index.html 的 data-value 一致）
GENDERS = {"男生", "女生"}
ONLINE_CHANNELS = {"微信小程序", "微信群 / QQ群", "微信公众号 / 朋友圈", "手机网页端"}
ONLINE_NEEDS_OPTIONS = {
    "二手闲置/教材转让",
    "校园表白墙/失物招领/求助墙",
    "期末资料/网课资源/考证题库",
    "外卖/打车/游玩拼单群",
    "靠谱兼职/校内实习信息",
    "其他",
}
OFFLINE_NEEDS_OPTIONS = {
    "代拿快递/外卖送上楼",
    "资料打印/装订送上门",
    "宿舍楼下平价零食/饮品站",
    "电脑清灰/手机贴膜/数码维修",
    "毕业季行李打包/寄件/物品回收",
    "其他",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("survey")

if not os.environ.get("ADMIN_PASSWORD"):
    log.warning("ADMIN_PASSWORD 未配置，使用默认密码 admin123（仅供本地测试，生产环境务必配置）")
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    log.warning("SECRET_KEY 未配置，已随机生成（重启后登录态失效，生产环境请配置固定值）")
if not (HF_BACKUP_REPO and HF_TOKEN):
    log.warning(
        "HF_BACKUP_REPO / HF_TOKEN 未完整配置，备份功能关闭，"
        "数据只保存在本地 SQLite（Space 重启可能丢失）"
    )

# ---------------------------------------------------------------------------
# Flask 应用
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 7  # 登录态保留 7 天
# Space 部署在反代后面，需要用 X-Forwarded-For 取真实客户端 IP，
# 否则所有请求都会被当成同一个代理 IP，频率限制会被全校共享。
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# ---------------------------------------------------------------------------
# 锁
# ---------------------------------------------------------------------------

# 写锁：串行化所有写操作（提交、删除、批量删除），保证 SQLite 文件
# 在备份上传时是完整一致的。
db_write_lock = threading.Lock()
# 备份锁：保证同一时间只有一个上传在进行。
backup_lock = threading.Lock()
# 限流锁：保护内存中的限流表。
rate_lock = threading.Lock()

# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    ip TEXT,
    user_agent TEXT,
    gender TEXT NOT NULL,
    online_direct TEXT NOT NULL,
    online_needs TEXT NOT NULL,
    online_needs_other TEXT,
    online_channel TEXT NOT NULL,
    offline_direct TEXT NOT NULL,
    offline_needs TEXT NOT NULL,
    offline_needs_other TEXT
);
CREATE INDEX IF NOT EXISTS idx_submissions_created_at ON submissions (created_at DESC);
"""


def get_db():
    """请求级数据库连接（读操作用；写操作走 get_write_conn）。"""
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def row_to_dict(row):
    """数据库行 -> API 字典。多选字段解析为 JSON 数组，字段名用驼峰
    （与提交接口的 onlineDirect / onlineNeeds 等保持一致）。"""
    d = dict(row)
    for key in ("online_needs", "offline_needs"):
        try:
            d[key] = json.loads(d[key] or "[]")
        except (ValueError, TypeError):
            d[key] = []
    return {
        "id": d["id"],
        "created_at": d["created_at"],
        "ip": d["ip"],
        "user_agent": d["user_agent"],
        "gender": d["gender"],
        "onlineDirect": d["online_direct"],
        "onlineNeeds": d["online_needs"],
        "onlineNeedsOther": d["online_needs_other"],
        "onlineChannel": d["online_channel"],
        "offlineDirect": d["offline_direct"],
        "offlineNeeds": d["offline_needs"],
        "offlineNeedsOther": d["offline_needs_other"],
    }


# ---------------------------------------------------------------------------
# Hugging Face 数据集备份
# ---------------------------------------------------------------------------

def backup_enabled():
    return bool(HF_BACKUP_REPO and HF_TOKEN)


def backup_upload():
    """把当前 SQLite 文件上传到 HF 数据集仓库（调用方持有 db_write_lock）。

    失败只记日志，不影响本次请求——数据已在本地 SQLite 里。
    """
    if not backup_enabled():
        return
    with backup_lock:
        try:
            from huggingface_hub import HfApi

            HfApi(token=HF_TOKEN).upload_file(
                path_or_fileobj=DB_PATH,
                path_in_repo=HF_REPO_FILENAME,
                repo_id=HF_BACKUP_REPO,
                repo_type="dataset",
            )
            log.info("备份成功：%s -> %s", DB_PATH, HF_BACKUP_REPO)
        except Exception as exc:  # noqa: BLE001 - 备份失败不能影响主流程
            log.warning("备份失败（数据仍在本地 SQLite）：%s", exc)


def backup_restore():
    """启动时：如果本地没有数据库文件，尝试从 HF 数据集仓库下载恢复。"""
    if os.path.exists(DB_PATH):
        return
    if not backup_enabled():
        return
    try:
        from huggingface_hub import hf_hub_download

        os.makedirs(DATA_DIR, exist_ok=True)
        hf_hub_download(
            repo_id=HF_BACKUP_REPO,
            filename=HF_REPO_FILENAME,
            repo_type="dataset",
            token=HF_TOKEN,
            local_dir=DATA_DIR,
            local_dir_use_symlinks=False,
        )
        log.info("已从备份仓库恢复数据库：%s", HF_BACKUP_REPO)
    except Exception as exc:  # noqa: BLE001 - 首次部署仓库里还没有文件是正常的
        log.warning("备份恢复失败（将使用空数据库）：%s", exc)


# ---------------------------------------------------------------------------
# 频率限制（内存滑动窗口，单进程）
# ---------------------------------------------------------------------------

_rate_buckets = defaultdict(deque)  # ip -> deque[时间戳]


def rate_limited(ip):
    """该 IP 在窗口内提交次数是否已满。未满则记一次并返回 False。"""
    now = datetime.now(timezone.utc).timestamp()
    with rate_lock:
        bucket = _rate_buckets[ip]
        while bucket and bucket[0] <= now - RATE_LIMIT_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= RATE_LIMIT_MAX:
            return True
        bucket.append(now)
        return False


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def _is_nonempty_str(value):
    return isinstance(value, str) and bool(value.strip())


def validate_submission(data):
    """校验提交内容，返回 (cleaned, error)。error 为 None 时校验通过。"""
    if not isinstance(data, dict):
        return None, "请求数据格式错误"

    # 蜜罐：正常用户看不到这个字段，机器人填了就拒绝。
    honeypot = data.get("honeypot", "")
    if isinstance(honeypot, str) and honeypot.strip():
        return None, "提交失败，请重试"

    gender = data.get("gender")
    if not _is_nonempty_str(gender) or gender.strip() not in GENDERS:
        return None, "第 1 题请选择性别"

    online_direct = data.get("onlineDirect")
    if not _is_nonempty_str(online_direct):
        return None, "第 2 题请填写你希望的线上服务"
    if len(online_direct.strip()) > MAX_TEXT_LEN:
        return None, "第 2 题内容过长"

    online_needs = data.get("onlineNeeds")
    if not isinstance(online_needs, list) or not online_needs:
        return None, "第 3 题请至少选择一项线上服务"
    if any(not isinstance(v, str) or v not in ONLINE_NEEDS_OPTIONS for v in online_needs):
        return None, "第 3 题选项无效"

    online_channel = data.get("onlineChannel")
    if not _is_nonempty_str(online_channel) or online_channel.strip() not in ONLINE_CHANNELS:
        return None, "第 4 题请选择线上使用方式"

    offline_direct = data.get("offlineDirect")
    if not _is_nonempty_str(offline_direct):
        return None, "第 5 题请填写你希望的线下服务"
    if len(offline_direct.strip()) > MAX_TEXT_LEN:
        return None, "第 5 题内容过长"

    offline_needs = data.get("offlineNeeds")
    if not isinstance(offline_needs, list) or not offline_needs:
        return None, "第 6 题请至少选择一项线下服务"
    if any(not isinstance(v, str) or v not in OFFLINE_NEEDS_OPTIONS for v in offline_needs):
        return None, "第 6 题选项无效"

    online_other = data.get("onlineNeedsOther") or ""
    offline_other = data.get("offlineNeedsOther") or ""
    if not isinstance(online_other, str) or not isinstance(offline_other, str):
        return None, "补充内容格式错误"
    if len(online_other) > MAX_OTHER_LEN or len(offline_other) > MAX_OTHER_LEN:
        return None, "补充内容过长"

    cleaned = {
        "gender": gender.strip(),
        "online_direct": online_direct.strip(),
        "online_needs": sorted(set(online_needs)),
        "online_needs_other": online_other.strip(),
        "online_channel": online_channel.strip(),
        "offline_direct": offline_direct.strip(),
        "offline_needs": sorted(set(offline_needs)),
        "offline_needs_other": offline_other.strip(),
    }
    return cleaned, None


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(os.path.join(BASE_DIR, "static"), "index.html")


@app.route("/admin")
def admin_page():
    return render_template("admin.html")


@app.route("/health")
def health():
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 问卷提交接口
# ---------------------------------------------------------------------------

@app.route("/api/submit", methods=["POST"])
def submit():
    ip = request.remote_addr or ""
    # 先做蜜罐+校验（不合规的请求不计入限流次数）
    data = request.get_json(silent=True)
    cleaned, error = validate_submission(data)
    if error:
        return jsonify({"ok": False, "error": error}), 400

    # 宽松限流：每 IP 每 10 分钟 60 次。一个班同时交卷没问题。
    if rate_limited(ip):
        return jsonify({"ok": False, "error": "提交太频繁了，请稍后再试"}), 429

    sid = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    user_agent = request.headers.get("User-Agent", "")[:500]

    with db_write_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(
                """
                INSERT INTO submissions
                (id, created_at, ip, user_agent, gender, online_direct,
                 online_needs, online_needs_other, online_channel,
                 offline_direct, offline_needs, offline_needs_other)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sid,
                    created_at,
                    ip,
                    user_agent,
                    cleaned["gender"],
                    cleaned["online_direct"],
                    json.dumps(cleaned["online_needs"], ensure_ascii=False),
                    cleaned["online_needs_other"],
                    cleaned["online_channel"],
                    cleaned["offline_direct"],
                    json.dumps(cleaned["offline_needs"], ensure_ascii=False),
                    cleaned["offline_needs_other"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
        # 写锁内上传备份，保证文件一致
        backup_upload()

    log.info("新提交 %s，IP %s", sid, ip)
    return jsonify({"ok": True, "id": sid})


# ---------------------------------------------------------------------------
# 管理员登录
# ---------------------------------------------------------------------------

def require_admin():
    if not session.get("admin"):
        abort(401)


@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    data = request.get_json(silent=True) or {}
    username = data.get("username", "")
    password = data.get("password", "")
    if (isinstance(username, str) and isinstance(password, str)
            and secrets.compare_digest(username, ADMIN_USER)
            and secrets.compare_digest(password, ADMIN_PASSWORD)):
        session["admin"] = True
        session.permanent = True
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "用户名或密码不正确"}), 401


@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin", None)
    return jsonify({"ok": True})


@app.route("/api/admin/me", methods=["GET"])
def admin_me():
    return jsonify({"logged_in": bool(session.get("admin"))})


# ---------------------------------------------------------------------------
# 管理员接口
# ---------------------------------------------------------------------------

def _build_filters():
    """从 query 参数拼筛选条件，返回 (where_sql, params)。"""
    clauses = []
    params = []
    q = (request.args.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        clauses.append(
            "(gender LIKE ? OR online_direct LIKE ? OR online_needs LIKE ? "
            "OR online_needs_other LIKE ? OR online_channel LIKE ? "
            "OR offline_direct LIKE ? OR offline_needs LIKE ? "
            "OR offline_needs_other LIKE ?)"
        )
        params.extend([like] * 8)
    gender = (request.args.get("gender") or "").strip()
    if gender:
        clauses.append("gender = ?")
        params.append(gender)
    channel = (request.args.get("channel") or "").strip()
    if channel:
        clauses.append("online_channel = ?")
        params.append(channel)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


@app.route("/api/admin/submissions", methods=["GET"])
def admin_list():
    require_admin()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        per_page = int(request.args.get("per_page", 20))
    except ValueError:
        per_page = 20
    per_page = min(max(per_page, 1), 100)

    where, params = _build_filters()
    db = get_db()
    total = db.execute(
        f"SELECT COUNT(*) FROM submissions {where}", params
    ).fetchone()[0]
    rows = db.execute(
        f"""SELECT * FROM submissions {where}
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?""",
        params + [per_page, (page - 1) * per_page],
    ).fetchall()
    return jsonify(
        {
            "ok": True,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page if total else 0,
            "items": [row_to_dict(r) for r in rows],
        }
    )


@app.route("/api/admin/submissions/<sid>", methods=["GET"])
def admin_detail(sid):
    require_admin()
    db = get_db()
    row = db.execute("SELECT * FROM submissions WHERE id = ?", (sid,)).fetchone()
    if row is None:
        return jsonify({"ok": False, "error": "记录不存在"}), 404
    return jsonify({"ok": True, "item": row_to_dict(row)})


@app.route("/api/admin/submissions/<sid>", methods=["DELETE"])
def admin_delete(sid):
    require_admin()
    with db_write_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.execute("DELETE FROM submissions WHERE id = ?", (sid,))
            conn.commit()
            deleted = cur.rowcount
        finally:
            conn.close()
        if deleted:
            backup_upload()
    if not deleted:
        return jsonify({"ok": False, "error": "记录不存在"}), 404
    return jsonify({"ok": True, "deleted": 1})


@app.route("/api/admin/submissions/delete-batch", methods=["POST"])
def admin_delete_batch():
    require_admin()
    data = request.get_json(silent=True) or {}
    ids = data.get("ids")
    if not isinstance(ids, list) or not ids:
        return jsonify({"ok": False, "error": "请提供要删除的记录 id 列表"}), 400
    ids = [i for i in ids if isinstance(i, str)][:500]
    if not ids:
        return jsonify({"ok": False, "error": "id 列表为空"}), 400
    with db_write_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            placeholders = ",".join("?" for _ in ids)
            cur = conn.execute(
                f"DELETE FROM submissions WHERE id IN ({placeholders})", ids
            )
            conn.commit()
            deleted = cur.rowcount
        finally:
            conn.close()
        if deleted:
            backup_upload()
    return jsonify({"ok": True, "deleted": deleted})


@app.route("/api/admin/export.csv", methods=["GET"])
def admin_export_csv():
    require_admin()
    where, params = _build_filters()
    db = get_db()
    rows = db.execute(
        f"SELECT * FROM submissions {where} ORDER BY created_at DESC, id DESC",
        params,
    ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "提交时间(UTC)",
            "性别",
            "希望的线上服务（直接填写）",
            "需要的线上服务选项",
            "线上服务其他补充",
            "线上使用方式",
            "希望的线下服务（直接填写）",
            "需要的线下服务选项",
            "线下服务其他补充",
            "IP",
            "记录ID",
        ]
    )
    for row in rows:
        d = row_to_dict(row)
        writer.writerow(
            [
                d["created_at"],
                d["gender"],
                d["onlineDirect"],
                "、".join(d["onlineNeeds"]),
                d["onlineNeedsOther"],
                d["onlineChannel"],
                d["offlineDirect"],
                "、".join(d["offlineNeeds"]),
                d["offlineNeedsOther"],
                d["ip"],
                d["id"],
            ]
        )
    # UTF-8 BOM，Excel 直接打开不乱码
    content = "\ufeff" + output.getvalue()
    filename = "survey-export-%s.csv" % datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return app.response_class(
        content,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/admin/stats", methods=["GET"])
def admin_stats():
    require_admin()
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
    gender_rows = db.execute(
        "SELECT gender, COUNT(*) AS c FROM submissions GROUP BY gender"
    ).fetchall()
    channel_rows = db.execute(
        "SELECT online_channel, COUNT(*) AS c FROM submissions GROUP BY online_channel"
    ).fetchall()
    return jsonify(
        {
            "ok": True,
            "total": total,
            "gender": {r["gender"]: r["c"] for r in gender_rows},
            "channel": {r["online_channel"]: r["c"] for r in channel_rows},
        }
    )


# ---------------------------------------------------------------------------
# 错误处理（统一返回 JSON）
# ---------------------------------------------------------------------------

@app.errorhandler(400)
def handle_400(error):
    return jsonify({"ok": False, "error": "请求数据错误"}), 400


@app.errorhandler(401)
def handle_401(error):
    return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401


@app.errorhandler(404)
def handle_404(error):
    return jsonify({"ok": False, "error": "不存在"}), 404


@app.errorhandler(429)
def handle_429(error):
    return jsonify({"ok": False, "error": "请求太频繁，请稍后再试"}), 429


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

backup_restore()
init_db()

if __name__ == "__main__":
    # 本地开发用；生产部署用 gunicorn（见 Dockerfile）
    app.run(host="0.0.0.0", port=7860)
