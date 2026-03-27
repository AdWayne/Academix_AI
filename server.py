import os
import sqlite3
import secrets
import hashlib
import binascii
from datetime import datetime
import fitz

from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

# =========================
# INIT
# =========================

load_dotenv()

if not os.getenv("OPENAI_API_KEY"):
    raise ValueError("OPENAI_API_KEY не найден в .env")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# DATABASE
# =========================

class UserDatabase:
    def __init__(self, db_name="academic_users.db"):
        self.conn = sqlite3.connect(db_name, check_same_thread=False)
        self.create_tables()

    def create_tables(self):
        cursor = self.conn.cursor()

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            created_at TEXT
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            file_name TEXT,
            result TEXT,
            created_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """)

        self.conn.commit()


    def _hash_password(self, password: str, salt_hex: str) -> str:
        salt = binascii.unhexlify(salt_hex)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200000)
        return binascii.hexlify(dk).decode()

    # =========================
    # REGISTER
    # =========================

    def register_user(self, username: str, password: str):
        cursor = self.conn.cursor()

        cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
        if cursor.fetchone():
            return False, "Пользователь уже существует."

        salt_hex = binascii.hexlify(os.urandom(16)).decode()
        password_hash = self._hash_password(password, salt_hex)

        cursor.execute("""
            INSERT INTO users (username, password_hash, salt, created_at)
            VALUES (?, ?, ?, ?)
        """, (username, password_hash, salt_hex, datetime.now().isoformat()))

        self.conn.commit()
        return True, "Регистрация успешна."

    # =========================
    # LOGIN
    # =========================

    def login_user(self, username: str, password: str):
        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT id, password_hash, salt
            FROM users
            WHERE username = ?
        """, (username,))

        row = cursor.fetchone()
        if not row:
            return False, None, "Пользователь не найден."

        user_id, stored_hash, salt_hex = row
        candidate_hash = self._hash_password(password, salt_hex)

        if candidate_hash != stored_hash:
            return False, None, "Неверный пароль."

        return True, user_id, "Вход выполнен."

    # =========================
    # SAVE ANALYSIS
    # =========================

    def save_analysis(self, user_id, file_name, result):
        cursor = self.conn.cursor()

        cursor.execute("""
            INSERT INTO analyses (user_id, file_name, result, created_at)
            VALUES (?, ?, ?, ?)
        """, (user_id, file_name, result, datetime.now().isoformat()))

        self.conn.commit()


db = UserDatabase()


TOKENS = {}  # token -> user_id


class AuthIn(BaseModel):
    username: str
    password: str


@app.post("/auth/register")
def register(data: AuthIn):
    ok, msg = db.register_user(data.username, data.password)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)

    ok2, user_id, _ = db.login_user(data.username, data.password)
    token = secrets.token_hex(24)
    TOKENS[token] = user_id

    return {"token": token}

@app.post("/auth/login")
def login(data: AuthIn):
    ok, user_id, msg = db.login_user(data.username, data.password)
    if not ok:
        raise HTTPException(status_code=401, detail=msg)

    token = secrets.token_hex(24)
    TOKENS[token] = user_id

    return {"token": token}

def get_user_id_from_auth(authorization: str | None):
    if not authorization:
        raise HTTPException(status_code=401, detail="Нет токена.")

    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Неверный формат токена.")

    token = authorization.replace("Bearer ", "").strip()
    user_id = TOKENS.get(token)

    if not user_id:
        raise HTTPException(status_code=401, detail="Токен недействителен.")

    return user_id


llm = ChatOpenAI(
    model="gpt-4o-mini",
    temperature=0.2
)

def analyze_text(text_content):
    messages = [
        SystemMessage(content="Ты научный аналитик. Сделай академический анализ."),
        HumanMessage(content=text_content)
    ]

    response = llm.invoke(messages)
    return response.content


def extract_text_from_pdf(file_bytes):
    text = ""
    with fitz.open(stream=file_bytes, filetype="pdf") as doc:
        for page in doc:
            text += page.get_text()
    return text

@app.post("/chat")
async def chat(
    authorization: str | None = Header(default=None),
    text: str | None = Form(default=None),
    file: UploadFile | None = File(default=None)
):
    user_id = get_user_id_from_auth(authorization)

    if not text and not file:
        raise HTTPException(status_code=400, detail="Нет данных.")

    if file:
        contents = await file.read()
        raw_text = extract_text_from_pdf(contents)
        result = analyze_text(raw_text[:15000])
        db.save_analysis(user_id, file.filename, result)
        return {"reply": result}

    if text:
        result = analyze_text(text)
        db.save_analysis(user_id, "text_input", result)
        return {"reply": result}

    return {"reply": "Ошибка."}

from fastapi.responses import FileResponse

@app.get("/")
def site():
    return FileResponse("index.html")