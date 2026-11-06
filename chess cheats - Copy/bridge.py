import json
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer

import chess
import chess.engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bridge")

cfg = json.load(open("config.json"))
TARGETS = {n.lower() for n in cfg.get("target_opponents", [])}

# ── Load both engines up front ────────────────────────────────────────────
log.info("Loading Maia: %s", " ".join(cfg["maia"]["cmd"]))
try:
    maia = chess.engine.SimpleEngine.popen_uci(cfg["maia"]["cmd"])
    log.info("Maia loaded OK — engine name = %r", maia.id.get("name", "?"))
except Exception:
    log.exception("Maia FAILED to load — non-target games will not work until fixed")
    maia = None

log.info("Loading Stockfish: %s", " ".join(cfg["stockfish"]["cmd"]))
try:
    stockfish = chess.engine.SimpleEngine.popen_uci(cfg["stockfish"]["cmd"])
    log.info("Stockfish loaded OK — engine name = %r", stockfish.id.get("name", "?"))
except Exception:
    log.exception("Stockfish FAILED to load — target games will not work")
    stockfish = None

maia_limits = [
    chess.engine.Limit(nodes=cfg["maia"].get("nodes", 1)),
    chess.engine.Limit(time=0.2),  # fallback if nodes=1 is ignored
]
sf_limit = chess.engine.Limit(time=cfg["stockfish"].get("time", 0.1))

stats = {"maia_ok": 0, "stockfish_ok": 0, "maia_fail": 0, "sf_fail": 0}


# ── Routing ───────────────────────────────────────────────────────────────
def route(opponent: str):
    if opponent and opponent.lower() in TARGETS:
        return stockfish, [sf_limit], "stockfish"
    return maia, maia_limits, "maia"


def get_move(fen: str, opponent: str) -> dict:
    label = "maia"
    try:
        board = chess.Board(fen)
    except Exception as e:
        log.error("bad FEN from %r: %s", opponent, e)
        return {"uci": None, "engine": label, "error": f"bad_fen: {e}"}

    engine, limits, label = route(opponent)

    if engine is None:
        log.error("%s not loaded — cannot move for %r", label, opponent)
        return {"uci": None, "engine": label, "error": f"{label}_not_loaded"}

    log.info("→ %s for %r (turn=%s)",
             label, opponent, "white" if board.turn else "black")

    info = None
    last_err = None
    for attempt, limit in enumerate(limits, 1):
        try:
            info = engine.analyse(board, limit)
            pv = info.get("pv", [])
            if not pv:
                log.warning("%s attempt %d returned empty PV for %r",
                            label, attempt, opponent)
                last_err = "empty_pv"
                info = None
                continue
            break
        except Exception as e:
            log.exception("%s attempt %d failed for %r", label, attempt, opponent)
            last_err = str(e)
            info = None

    if info is None:
        stats["maia_fail" if label == "maia" else "sf_fail"] += 1
        log.error("All %s attempts failed for %r — returning null", label, opponent)
        return {"uci": None, "engine": label,
                "error": last_err or "all_attempts_failed"}

    stats["maia_ok" if label == "maia" else "stockfish_ok"] += 1
    pv = info["pv"]

    show_eval = label == "stockfish"
    eval_cp, mate = None, None
    if show_eval:
        try:
            score = info["score"].pov(board.turn)
            eval_cp = score.score(mate_score=100000)
            mate = score.mate()
            if eval_cp is not None:
                eval_cp = round(eval_cp / 100, 2)
        except Exception:
            pass

    return {
        "uci": pv[0].uci(),
        "engine": label,
        "eval": eval_cp,
        "mate": mate,
        "pv": " ".join(m.uci() for m in pv[:6]),
    }


# ── HTTP plumbing ────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _reply(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._reply({
                "status": "ok",
                "maia_loaded": maia is not None,
                "stockfish_loaded": stockfish is not None,
                "maia_name": maia.id.get("name") if maia else None,
                "stockfish_name": stockfish.id.get("name") if stockfish else None,
                "targets": sorted(TARGETS),
                "stats": stats,
            })
        else:
            self._reply({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/move":
            self._reply({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            msg = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._reply({"uci": None, "error": "bad_json"}, 400)
            return
        self._reply(get_move(msg.get("fen", ""), msg.get("opponent", "")))


def shutdown():
    for eng in (maia, stockfish):
        if eng is None:
            continue
        try:
            eng.quit()
        except Exception:
            pass


if __name__ == "__main__":
    log.info("Bridge on http://127.0.0.1:8765 — GET /health, POST /move")
    try:
        HTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
    finally:
        shutdown()