"""
海賊じゃんけん 対人対戦サーバー（FastAPI + WebSocket）

- 2人が同じ「部屋コード」で入室すると対戦できます。
- ゲームの進行と勝敗判定はすべてこのサーバー側で行い、
  2人のブラウザに同じ結果を配ります（不正やズレを防ぐため）。

起動（テスト用）:
    uvicorn server:app --host 0.0.0.0 --port 8000

必要なもの:  pip install -r requirements.txt
"""
import asyncio
import json
import random
import string
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

app = FastAPI()
BASE = Path(__file__).parent

# ===== ルール定数 =====
BEATS = {"goo": "choki", "choki": "paa", "paa": "goo"}  # key が value に勝つ
TOTAL_ROUNDS = 5
PICK_SECONDS = 20  # 1手を選ぶ制限時間（秒）


def make_deck():
    """グー2・チョキ2・パー2 の6枚。★は1枚だけランダムに付く。"""
    types = ["goo", "choki", "paa", "goo", "choki", "paa"]
    deck = [{"type": t, "star": False, "used": False} for t in types]
    deck[random.randrange(6)]["star"] = True
    return deck


def judge(a, b):
    """a の視点で win / lose / draw を返す。"""
    if a == b:
        return "draw"
    return "win" if BEATS[a] == b else "lose"


class Player:
    def __init__(self, ws, name):
        self.ws = ws
        self.name = (name or "海賊").strip()[:12] or "海賊"
        self.deck = make_deck()
        self.star = 0          # 累積★（勝敗を決める得点）
        self.wins = 0          # 勝った回数（クラウン表示用）
        self.strength = 1500   # 強さ（レート）
        self.games = 0
        self.ready = False
        self.ready_next = False
        self.rematch = False
        self.pick = None       # このラウンドで選んだ手札の番号
        self.timeouts = 0      # 時間切れ回数（2回で自動負け）
        self.force_lose = False
        self.delta = 0
        self.connected = True

    async def send(self, obj):
        try:
            await self.ws.send_text(json.dumps(obj, ensure_ascii=False))
        except Exception:
            pass


class Room:
    def __init__(self, code):
        self.code = code
        self.players = []
        self.phase = "lobby"   # lobby -> ready -> playing -> reveal -> over
        self.round = 1
        self.first_round = True
        self.timer = None

    def opp(self, p):
        for q in self.players:
            if q is not p:
                return q
        return None


rooms = {}


def gen_code():
    while True:
        c = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
        if c not in rooms:
            return c


def view(p):
    """相手にも見せてよい情報（手札は両者見えるルール）。"""
    return {
        "name": p.name, "deck": p.deck, "star": p.star, "wins": p.wins,
        "strength": p.strength, "games": p.games,
        "ready": p.ready, "readyNext": p.ready_next, "connected": p.connected,
    }


async def send_state(room):
    for p in room.players:
        o = room.opp(p)
        await p.send({
            "t": "state", "phase": room.phase, "round": room.round,
            "code": room.code, "players": len(room.players),
            "you": view(p), "opp": (view(o) if o else None),
        })


# ===== タイマー =====
def cancel_timer(room):
    if room.timer and not room.timer.done():
        room.timer.cancel()
    room.timer = None


def start_timer(room):
    cancel_timer(room)
    room.timer = asyncio.create_task(round_timer(room, room.round))


async def round_timer(room, rnd):
    try:
        await asyncio.sleep(PICK_SECONDS)
    except asyncio.CancelledError:
        return
    if room.phase != "playing" or room.round != rnd:
        return
    # 時間切れ：まだ選んでいない人を自動処理
    for p in room.players:
        if p.pick is None:
            p.timeouts += 1
            remain = [i for i, c in enumerate(p.deck) if not c["used"]]
            if remain:
                p.pick = random.choice(remain)   # 1回目：ランダム
            if p.timeouts >= 2:
                p.force_lose = True               # 2回目：自動的に負け
    await try_resolve(room)


# ===== 進行 =====
async def start_match(room):
    for p in room.players:
        p.deck = make_deck()
        p.star = 0
        p.wins = 0
        p.pick = None
        p.timeouts = 0
        p.ready_next = False
        p.force_lose = False
        p.rematch = False
    room.round = 1
    room.first_round = True
    await start_round(room)


async def start_round(room):
    for p in room.players:
        p.pick = None
        p.ready_next = False
        p.force_lose = False
    room.phase = "playing"
    await send_state(room)
    for p in room.players:
        await p.send({"t": "round_start", "round": room.round, "first": room.first_round})
    room.first_round = False
    start_timer(room)


async def on_pick(room, p, idx):
    if room.phase != "playing" or p.pick is not None:
        return
    if not isinstance(idx, int) or idx < 0 or idx >= 6 or p.deck[idx]["used"]:
        return
    p.pick = idx
    await p.send({"t": "you_picked", "index": idx})
    o = room.opp(p)
    if o:
        await o.send({"t": "opp_picked"})
    await try_resolve(room)


async def try_resolve(room):
    if len(room.players) == 2 and all(pl.pick is not None for pl in room.players):
        cancel_timer(room)
        await resolve(room)


def apply_score(winner, wc, lc):
    """勝った側に加点（負けても減らない）。"""
    winner.wins += 1
    if wc["star"] and lc["star"]:
        winner.star += 3       # ★同士で勝ち
    elif wc["star"]:
        winner.star += 2       # ★でノーマルに勝ち
    else:
        winner.star += 1       # ノーマル勝ち


async def resolve(room):
    a, b = room.players
    ac = a.deck[a.pick]
    bc = b.deck[b.pick]
    ac["used"] = True
    bc["used"] = True

    if a.force_lose and b.force_lose:
        res = "draw"
    elif a.force_lose:
        res = "lose"
    elif b.force_lose:
        res = "win"
    else:
        res = judge(ac["type"], bc["type"])  # a の視点

    if res == "win":
        apply_score(a, ac, bc)
    elif res == "lose":
        apply_score(b, bc, ac)

    room.phase = "reveal"
    await send_state(room)  # 使用済み・★をパネル/手札に反映

    inv = {"win": "lose", "lose": "win", "draw": "draw"}
    await a.send({"t": "reveal",
                  "you": {"type": ac["type"], "star": ac["star"]},
                  "opp": {"type": bc["type"], "star": bc["star"]},
                  "result": res})
    await b.send({"t": "reveal",
                  "you": {"type": bc["type"], "star": bc["star"]},
                  "opp": {"type": ac["type"], "star": ac["star"]},
                  "result": inv[res]})


async def on_ready_next(room, p):
    if room.phase != "reveal":
        return
    p.ready_next = True
    if len(room.players) == 2 and all(pl.ready_next for pl in room.players):
        if room.round >= TOTAL_ROUNDS:
            await game_over(room)
        else:
            room.round += 1
            await start_round(room)


async def game_over(room, forced_loser=None):
    a, b = room.players
    if forced_loser is a:
        ra, rb = "lose", "win"
    elif forced_loser is b:
        ra, rb = "win", "lose"
    elif a.star > b.star:
        ra, rb = "win", "lose"
    elif a.star < b.star:
        ra, rb = "lose", "win"
    else:
        ra = rb = "draw"
    for p, r in [(a, ra), (b, rb)]:
        p.games += 1
        p.delta = 16 if r == "win" else (-16 if r == "lose" else 0)
        p.strength += p.delta
    room.phase = "over"
    cancel_timer(room)
    await send_state(room)
    for p, r in [(a, ra), (b, rb)]:
        o = room.opp(p)
        await p.send({"t": "game_over", "result": r,
                      "yourStar": p.star, "oppStar": (o.star if o else 0),
                      "delta": p.delta})


async def on_ready(room, p):
    if room.phase != "ready":
        return
    p.ready = True
    await send_state(room)
    if len(room.players) == 2 and all(pl.ready for pl in room.players):
        await start_match(room)


async def on_rematch(room, p):
    if room.phase != "over":
        return
    p.rematch = True
    if len(room.players) == 2 and all(pl.rematch for pl in room.players):
        for pl in room.players:
            pl.ready = False
            pl.rematch = False
        room.phase = "ready"
        await send_state(room)


# ===== ルーティング =====
@app.get("/")
async def index():
    return FileResponse(BASE / "index.html")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    player = None
    room = None
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            t = msg.get("t")

            if t == "join" and player is None:
                name = msg.get("name", "")
                code = (msg.get("room") or "").strip().upper()
                if not code:
                    code = gen_code()
                room = rooms.get(code)
                if room is None:
                    room = Room(code)
                    rooms[code] = room
                if len(room.players) >= 2:
                    await ws.send_text(json.dumps(
                        {"t": "error", "msg": "その部屋は満員です"}, ensure_ascii=False))
                    room = None
                    continue
                player = Player(ws, name)
                room.players.append(player)
                await player.send({"t": "joined",
                                   "you": room.players.index(player),
                                   "code": room.code})
                if len(room.players) == 2:
                    room.phase = "ready"
                await send_state(room)

            elif player is None or room is None:
                continue
            elif t == "ready":
                await on_ready(room, player)
            elif t == "pick":
                await on_pick(room, player, msg.get("index"))
            elif t == "ready_next":
                await on_ready_next(room, player)
            elif t == "rematch":
                await on_rematch(room, player)
            elif t == "surrender":
                if room.phase in ("playing", "reveal"):
                    cancel_timer(room)
                    await game_over(room, forced_loser=player)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if room and player:
            player.connected = False
            cancel_timer(room)
            o = room.opp(player)
            if player in room.players:
                room.players.remove(player)
            if o:
                await o.send({"t": "opp_left"})
                room.phase = "lobby"
                await send_state(room)
            if not room.players:
                rooms.pop(room.code, None)
