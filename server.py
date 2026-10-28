
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional
 
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
 
# ──────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("GAME_DB_PATH", BASE_DIR / "game_records.db"))
ADMIN_KEY = os.getenv("ADMIN_KEY", "reset-1234")  # 발표 전 반드시 환경변수로 변경 권장
MAX_PLAY_TIME_SEC = 60 * 60 * 3                    # 3시간 초과 기록은 비정상으로 간주
 
app = FastAPI(title="살아남아 퇴근하기 API", version="1.0.0")
 
# CORS: 모바일 클라이언트/대시보드를 다른 포트·도메인에서 띄워도 호출 가능하도록 전부 허용
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
 
 
# ──────────────────────────────────────────────
# DB
# ──────────────────────────────────────────────
@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
 
 
def init_db() -> None:
    with get_conn() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")  # 동시 읽기/쓰기 안정성
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS game_records (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                player_name   TEXT    NOT NULL,
                result_type   TEXT    NOT NULL CHECK (result_type IN ('SUCCESS', 'FAILED')),
                play_time_sec INTEGER NOT NULL CHECK (play_time_sec >= 0),
                solved_quiz   INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT    NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rank ON game_records (result_type, play_time_sec, created_at)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON game_records (created_at)")
 
 
init_db()
 
 
# ──────────────────────────────────────────────
# 스키마
# ──────────────────────────────────────────────
class GameRecordIn(BaseModel):
    player_name: str = Field(..., min_length=1, max_length=20)
    result_type: Literal["SUCCESS", "FAILED"]
    play_time_sec: int = Field(..., ge=0, le=MAX_PLAY_TIME_SEC)
    solved_quiz: bool = False
 
    @field_validator("player_name")
    @classmethod
    def clean_name(cls, v: str) -> str:
        v = " ".join(v.split())  # 앞뒤/연속 공백 정리
        if not v:
            raise ValueError("player_name은 비어 있을 수 없습니다.")
        return v
 
 
class GameRecordOut(BaseModel):
    id: int
    player_name: str
    result_type: str
    play_time_sec: int
    solved_quiz: bool
    created_at: str
 
 
def row_to_record(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "player_name": row["player_name"],
        "result_type": row["result_type"],
        "play_time_sec": row["play_time_sec"],
        "solved_quiz": bool(row["solved_quiz"]),
        "created_at": row["created_at"],
    }
 
 
def now_utc_iso() -> str:
    # 프론트에서 로컬(KST)로 변환할 수 있게 UTC ISO 8601 + 'Z'로 저장
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
 
 
# ──────────────────────────────────────────────
# API
# ──────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {"status": "ok", "time": now_utc_iso()}
 
 
@app.post("/api/game/record", status_code=201)
def create_record(record: GameRecordIn):
    created_at = now_utc_iso()
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO game_records (player_name, result_type, play_time_sec, solved_quiz, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (record.player_name, record.result_type, record.play_time_sec, int(record.solved_quiz), created_at),
        )
        new_id = cur.lastrowid
 
        # 성공 기록이면 현재 순위도 함께 돌려줌 (게임 결과 화면에서 "현재 N위!" 표시용)
        rank: Optional[int] = None
        if record.result_type == "SUCCESS":
            rank = conn.execute(
                """
                SELECT COUNT(*) + 1 FROM game_records
                WHERE result_type = 'SUCCESS'
                  AND (play_time_sec < ? OR (play_time_sec = ? AND created_at < ?)
                       OR (play_time_sec = ? AND created_at = ? AND id < ?))
                """,
                (record.play_time_sec, record.play_time_sec, created_at,
                 record.play_time_sec, created_at, new_id),
            ).fetchone()[0]
 
    return {
        "ok": True,
        "record": {
            "id": new_id,
            "player_name": record.player_name,
            "result_type": record.result_type,
            "play_time_sec": record.play_time_sec,
            "solved_quiz": record.solved_quiz,
            "created_at": created_at,
        },
        "rank": rank,
    }
 
 
@app.get("/api/game/records")
def get_records(
    log_limit: int = Query(50, ge=1, le=500, description="최신 플레이 로그 개수"),
    top: int = Query(10, ge=1, le=100, description="리더보드 인원 수"),
):
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM game_records").fetchone()[0]
        success = conn.execute(
            "SELECT COUNT(*) FROM game_records WHERE result_type = 'SUCCESS'"
        ).fetchone()[0]
 
        # 리더보드: 성공자 중 빠른 순, 동률이면 먼저 달성한 사람 우선
        leaderboard_rows = conn.execute(
            """
            SELECT * FROM game_records
            WHERE result_type = 'SUCCESS'
            ORDER BY play_time_sec ASC, created_at ASC, id ASC
            LIMIT ?
            """,
            (top,),
        ).fetchall()
 
        log_rows = conn.execute(
            "SELECT * FROM game_records ORDER BY created_at DESC, id DESC LIMIT ?",
            (log_limit,),
        ).fetchall()
 
    leaderboard = [
        {"rank": i + 1, **row_to_record(r)} for i, r in enumerate(leaderboard_rows)
    ]
    best = leaderboard[0] if leaderboard else None
 
    return {
        "stats": {
            "total_players": total,
            "success_count": success,
            "failed_count": total - success,
            "success_rate": round(success / total * 100, 1) if total else 0.0,
            "best_time_sec": best["play_time_sec"] if best else None,
            "best_player": best["player_name"] if best else None,
        },
        "leaderboard": leaderboard,
        "logs": [row_to_record(r) for r in log_rows],
        "server_time": now_utc_iso(),
    }
 
 
@app.delete("/api/game/records")
def reset_records(x_admin_key: str = Header(default="")):
    """리허설 기록 초기화. 헤더 X-Admin-Key 필요."""
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="관리자 키가 올바르지 않습니다.")
    with get_conn() as conn:
        deleted = conn.execute("DELETE FROM game_records").rowcount
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'game_records'")
    return {"ok": True, "deleted": deleted}
 
 
# ──────────────────────────────────────────────
# 정적 파일 (dashboard.html / index.html / assets)
# 반드시 API 라우트 등록 이후에 마운트해야 /api 경로를 가리지 않습니다.
# ──────────────────────────────────────────────
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/dashboard.html")
 
 
app.mount("/", StaticFiles(directory=BASE_DIR, html=True), name="static")
 
 
def get_lan_ip() -> str:
    """같은 와이파이의 스마트폰이 접속할 이 PC의 내부 IP (실제 패킷은 보내지 않음)."""
    import socket
 
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()
 
 
if __name__ == "__main__":
    import uvicorn
 
    port = int(os.getenv("PORT", 8000))
    lan_ip = get_lan_ip()
    print("\n" + "=" * 56)
    print("  살아남아 퇴근하기 서버 실행 중  (종료: Ctrl+C)")
    print("-" * 56)
    print(f"  이 PC 대시보드   : http://localhost:{port}/dashboard.html")
    print(f"  다른 PC 대시보드 : http://{lan_ip}:{port}/dashboard.html")
    print(f"  스마트폰 게임    : http://{lan_ip}:{port}/index.html")
    print(f"  API 테스트       : http://localhost:{port}/docs")
    print("  ※ 아래 로그의 0.0.0.0 주소는 브라우저에서 열리지 않습니다.")
    print("=" * 56 + "\n")
 
    # host="0.0.0.0" = 모든 네트워크 인터페이스에서 접속 허용 (스마트폰 접속에 필요)
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)