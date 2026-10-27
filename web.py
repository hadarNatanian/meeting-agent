"""ממשק דפדפן לאג'נט – שרת קטן שמבוסס רק על הספרייה הסטנדרטית של פייתון.

הרצה:  python web.py      ואז נפתח הדפדפן בכתובת http://localhost:8000

אותו אג'נט בדיוק (agent.py + tools.py) רץ כאן ברקע. הדפדפן מושך את הצעדים בזמן אמת,
וכשהאג'נט שואל שאלת הבהרה – הריצה ממתינה עד שהמשתמש עונה בדף.
"""
import json
import shutil
import sys
import threading
import uuid
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent import Agent, make_brain
from main import DEMO
from memory import MemoryError_, load_memory
from tools import AgentState

ROOT = Path(__file__).parent
WORK = ROOT / "runs"
WEB_MEMORY = WORK / "web_memory.json"     # הדפדפן עובד על עותק – memory.json המקורי לא משתנה
PORT = 8000
ANSWER_TIMEOUT = 600                       # שניות להמתנה לתשובת המשתמש לפני שהשאלה נחשבת לא נענתה

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=True)
except ImportError:
    pass

RUNS: dict[str, dict] = {}
LOCK = threading.Lock()


def ensure_memory(reset=False):
    WORK.mkdir(exist_ok=True)
    if reset or not WEB_MEMORY.exists():
        shutil.copy(ROOT / "memory.json", WEB_MEMORY)


def start_run(payload: dict) -> str:
    goal = payload.get("goal")
    if goal not in ("prepare_meeting", "summarize_meeting"):
        raise ValueError("מטרה לא חוקית")
    topic = str(payload.get("topic", "")).strip()
    pids = [str(p) for p in payload.get("participant_ids", []) if str(p).strip()]
    if not topic or not pids:
        raise ValueError("יש למלא נושא ולבחור לפחות משתתף אחד")
    meeting_date = str(payload.get("date") or date.today().isoformat())
    date.fromisoformat(meeting_date)   # זורק ValueError אם לא תקין
    summary = str(payload.get("summary_text", ""))
    if goal == "summarize_meeting" and not summary.strip():
        raise ValueError("בסיכום פגישה יש להדביק את טקסט הסיכום")
    memory_path = ROOT / "samples/memory_corrupt.json" if payload.get("corrupt") else WEB_MEMORY

    run_id = uuid.uuid4().hex[:10]
    run = {"id": run_id, "status": "running", "events": [], "question": None, "result": None,
           "answer": None, "answer_event": threading.Event()}
    with LOCK:
        RUNS[run_id] = run

    def emit(text):
        with LOCK:
            run["events"].append(text)

    def ask(question, options):
        with LOCK:
            run["question"] = {"text": question, "options": options}
            run["status"] = "waiting"
            run["answer_event"].clear()
        got = run["answer_event"].wait(ANSWER_TIMEOUT)
        with LOCK:
            answer = run["answer"] if got else None
            run["question"], run["answer"], run["status"] = None, None, "running"
        emit(f"❓ {question} ← " + (f"המשתמש בחר: {answer}" if answer else "לא התקבלה תשובה"))
        return answer

    def work():
        try:
            brain, llm, mode = make_brain(bool(payload.get("offline")), notify=emit)
            emit(f"מצב הפעלה: {mode}")
            state = AgentState(goal=goal, topic=topic, participant_ids=pids, meeting_date=meeting_date,
                               memory_path=str(memory_path), summary_text=summary, answer_provider=ask, llm=llm)
            result = Agent(brain, printer=emit).run(state)
            final = {"status": result["status"], "stop_reason": result["stop_reason"],
                     "steps": len(result["steps"]), "report": result["report"]}
        except Exception as e:   # לא מפילים את השרת – מדווחים בדף
            final = {"status": "failed", "stop_reason": "error", "steps": 0,
                     "report": f"שגיאה לא צפויה: {type(e).__name__}: {e}"}
        with LOCK:
            run["result"], run["status"] = final, "done"

    threading.Thread(target=work, daemon=True).start()
    return run_id


def meta() -> dict:
    ensure_memory()
    try:
        mem = load_memory(WEB_MEMORY)
        participants, topics, error = mem["participants"], mem["topics"], None
    except MemoryError_ as e:
        participants, topics, error = [], [], str(e)
    _, _, mode = make_brain(False)
    scenarios = []
    for title, goal, topic, pids, summary_file, answers in DEMO:
        scenarios.append({"title": title, "goal": goal, "topic": topic, "participant_ids": pids,
                          "summary_text": (ROOT / summary_file).read_text(encoding="utf-8") if summary_file else "",
                          "corrupt": "פגום" in title})
    return {"participants": participants, "topics": topics, "mode": mode, "memory_error": error,
            "scenarios": scenarios, "default_date": "2026-10-27"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):   # שקט בטרמינל
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 200_000:
            raise ValueError("הבקשה גדולה מדי")
        data = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("גוף הבקשה חייב להיות אובייקט JSON")
        return data

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, (ROOT / "web" / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/meta":
            return self._send(200, meta())
        if self.path == "/api/memory":
            ensure_memory()
            return self._send(200, WEB_MEMORY.read_bytes())
        if self.path.startswith("/api/run/"):
            run = RUNS.get(self.path.split("/")[3])
            if not run:
                return self._send(404, {"error": "ריצה לא נמצאה"})
            with LOCK:
                view = {k: run[k] for k in ("id", "status", "events", "question", "result")}
            return self._send(200, view)
        self._send(404, {"error": "לא נמצא"})

    def do_POST(self):
        try:
            if self.path == "/api/run":
                return self._send(202, {"run_id": start_run(self._body())})
            if self.path == "/api/reset-memory":
                ensure_memory(reset=True)
                return self._send(200, {"ok": True})
            if self.path.startswith("/api/run/") and self.path.endswith("/answer"):
                run = RUNS.get(self.path.split("/")[3])
                if not run:
                    return self._send(404, {"error": "ריצה לא נמצאה"})
                answer = self._body().get("answer")
                with LOCK:
                    q = run["question"]
                    if not q or answer not in q["options"]:
                        return self._send(400, {"error": "אין שאלה פתוחה או שהתשובה אינה מהאפשרויות"})
                    run["answer"] = answer
                    run["answer_event"].set()
                return self._send(200, {"ok": True})
            self._send(404, {"error": "לא נמצא"})
        except (ValueError, json.JSONDecodeError) as e:
            self._send(400, {"error": str(e)})


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    port = next((int(a) for a in sys.argv[1:] if a.isdigit()), PORT)
    ensure_memory()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}"
    print(f"האג'נט זמין בדפדפן: {url}   (לעצירה: Ctrl+C)")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nהשרת נעצר.")


if __name__ == "__main__":
    main()
