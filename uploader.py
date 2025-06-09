import os
import time
from uuid import uuid4
from datetime import datetime

import pandas as pd
import streamlit as st
import psycopg2
from psycopg2.extras import RealDictCursor
from minio import Minio


class DatabaseManager:
    def __init__(self):
        self.config = {
            "host": os.environ.get("PG_HOST", "localhost"),
            "port": int(os.environ.get("PG_PORT", 5432)),
            "database": os.environ.get("PG_DATABASE", "postgres"),
            "user": os.environ.get("PG_USER", "postgres"),
            "password": os.environ.get("PG_PASSWORD", "postgres"),
        }

    def get_connection(self):
        return psycopg2.connect(**self.config)


db = DatabaseManager()


def init_db():
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS upload_logs (
                id SERIAL PRIMARY KEY,
                file_name TEXT,
                minio_path TEXT,
                status TEXT,
                message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def log_upload(file_name: str, minio_path: str) -> int:
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO upload_logs (file_name, minio_path, status) VALUES (%s,%s,%s) RETURNING id",
            (file_name, minio_path, "uploaded"),
        )
        log_id = cur.fetchone()[0]
        conn.commit()
    return log_id


def update_log(log_id: int, status: str, message: str | None = None):
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE upload_logs SET status=%s, message=%s, updated_at=NOW() WHERE id=%s",
            (status, message, log_id),
        )
        conn.commit()


def get_logs():
    with db.get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM upload_logs ORDER BY created_at DESC")
        return cur.fetchall()


def save_to_minio(file_obj, file_name: str) -> str:
    bucket = os.environ.get("MINIO_BUCKET", "uploads")
    client = Minio(
        endpoint=os.environ.get("MINIO_ENDPOINT", "localhost:9000"),
        access_key=os.environ.get("MINIO_ACCESS_KEY", "minioadmin"),
        secret_key=os.environ.get("MINIO_SECRET_KEY", "minioadmin"),
        secure=False,
    )
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
    object_name = f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex}_{file_name}"
    file_obj.seek(0)
    client.put_object(bucket, object_name, file_obj, length=-1, part_size=10 * 1024 * 1024)
    return object_name


def process_file(log_id: int, local_path: str):
    try:
        df = pd.read_excel(local_path)
        time.sleep(2)  # simulate heavy task
        update_log(log_id, "completed", f"Processed {len(df)} rows")
    except Exception as exc:
        update_log(log_id, "failed", str(exc))


init_db()

st.set_page_config(page_title="File Uploader", layout="wide")
st.title("📁 Data Importer")

tab_upload, tab_logs = st.tabs(["Upload", "Logs"])

with tab_upload:
    uploaded = st.file_uploader("Choose Excel file", type=["xlsx", "xls"])
    if uploaded:
        temp_path = f"/tmp/{uuid4().hex}_{uploaded.name}"
        with open(temp_path, "wb") as tmp:
            tmp.write(uploaded.getbuffer())
        minio_path = save_to_minio(open(temp_path, "rb"), uploaded.name)
        log_id = log_upload(uploaded.name, minio_path)
        st.success(f"File uploaded to Minio as {minio_path}. Log ID {log_id}")
        st.info("Processing in background...")
        import threading

        threading.Thread(target=process_file, args=(log_id, temp_path), daemon=True).start()

with tab_logs:
    if st.button("Refresh"):
        st.experimental_rerun()
    logs = get_logs()
    if logs:
        st.dataframe(pd.DataFrame(logs))
    else:
        st.info("No logs yet.")
