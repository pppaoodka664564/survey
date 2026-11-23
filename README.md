# 校园生活与服务问卷调查 —— 后端

Python 3 + Flask + SQLite，部署目标是 Hugging Face Docker Space（端口 7860）。

## 功能

- 问卷页 `/`：6 道题，提交后入库，成功才显示成功页
- 管理面板 `/admin`：登录后看统计、搜索/筛选、点开看全部答案、
  单条删除、批量删除、导出 CSV（UTF-8 带 BOM，Excel 可直接打开）
- 管理面板响应式：手机上是卡片列表，电脑上是表格

## 关键设计：同 IP 怎么办

一个学校很多学生共用一个出口 IP，所以：

- **IP 只记录，不去重、不封禁**，IP 不是唯一键，同一 IP 可以交任意多份
- 防刷只做两层：
  1. 蜜罐字段 `honeypot`（前端隐藏输入，填了就 400 拒绝）
  2. 宽松频率限制：每 IP 每 10 分钟最多 60 次提交（内存滑动窗口），
     超了返回 429。一个班同时交卷完全没问题。

## 数据备份（Space 重启不丢数据）

每次写入（提交、删除）后，在锁保护下把 SQLite 文件上传到
Hugging Face 数据集仓库；启动时如果本地没有数据库文件，就尝试
从仓库下载恢复。`HF_BACKUP_REPO` / `HF_TOKEN` 没配时，只用本地
SQLite 并打一条警告日志，不报错退出。

## 环境变量

| 变量 | 说明 | 默认 |
|---|---|---|
| `ADMIN_PASSWORD` | 管理员登录密码 | `admin123`（仅本地测试，生产务必配置） |
| `SECRET_KEY` | Flask session 签名密钥 | 随机生成（重启后登录态失效，生产请配固定值） |
| `HF_BACKUP_REPO` | 备份用 Hugging Face 数据集仓库名，如 `用户名/survey-backup` | 未配置时关闭备份 |
| `HF_TOKEN` | Hugging Face token（需对备份仓库有写权限） | 未配置时关闭备份 |

注意：不要把任何密码或 token 写进代码或配置文件，只通过环境变量传入。
Space 上在 Settings → Variables and secrets 里配置。

## 本地运行

```bash
cd survey
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 本地测试（不配 HF 变量时备份自动关闭，只用本地 SQLite）
export ADMIN_PASSWORD=你的管理员密码
python app.py
```

打开 http://127.0.0.1:7860 填问卷，http://127.0.0.1:7860/admin 进管理面板。

生产启动命令（Dockerfile 里已配好）：

```bash
gunicorn app:app --bind 0.0.0.0:7860 --workers 1 --threads 4
```

**必须是单 worker**：内存限流、SQLite 写锁、备份锁都在同一个进程内，
多 worker 会导致限流计数分散、备份竞态。

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 问卷页 |
| GET | `/admin` | 管理面板 |
| GET | `/health` | 健康检查 |
| POST | `/api/submit` | 提交问卷（JSON，见下） |
| POST | `/api/admin/login` | 管理员登录 `{password}` |
| POST | `/api/admin/logout` | 退出登录 |
| GET | `/api/admin/me` | 查登录态 |
| GET | `/api/admin/submissions?page=1&per_page=20&q=&gender=&channel=` | 分页列表（时间倒序，`q` 模糊搜各题答案） |
| GET | `/api/admin/submissions/<id>` | 单条详情 |
| DELETE | `/api/admin/submissions/<id>` | 删除单条（数据库同步删除） |
| POST | `/api/admin/submissions/delete-batch` | 批量删除 `{ids:[...]}` |
| GET | `/api/admin/export.csv` | 按当前筛选导出 CSV |
| GET | `/api/admin/stats` | 总数、性别分布、渠道分布 |

提交 JSON 格式：

```json
{
  "gender": "男生",
  "onlineDirect": "希望有二手教材交易群",
  "onlineNeeds": ["二手闲置/教材转让", "其他"],
  "onlineNeedsOther": "拼车群",
  "onlineChannel": "微信小程序",
  "offlineDirect": "希望宿舍楼下有代拿快递",
  "offlineNeeds": ["代拿快递/外卖送上楼"],
  "offlineNeedsOther": "",
  "honeypot": ""
}
```

6 题都必填，多选至少选 1 项；不合法返回 400 带中文错误信息。
成功返回 `{"ok": true, "id": "..."}`。

## 文件结构

```
survey/
├── app.py                 Flask 应用（接口、校验、限流、备份、登录）
├── static/
│   └── index.html         问卷前端（基于 Gemini 的 upload.html 改，最小改动）
├── templates/
│   └── admin.html         管理面板（响应式）
├── data/
│   └── survey.db          SQLite 数据库（运行时生成，不进版本库）
├── requirements.txt
├── Dockerfile
└── README.md
```
