FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p data

EXPOSE 7860

# 单 worker + 多线程：保证内存限流、SQLite 写锁、备份锁都在同一个进程内一致。
# 多 worker 会导致限流计数分散、SQLite 备份竞态，禁止改成多 worker。
# shell 形式 CMD：用 $PORT（Render 等平台注入），未设置时默认 7860。
CMD gunicorn app:app --bind 0.0.0.0:${PORT:-7860} --workers 1 --threads 4
