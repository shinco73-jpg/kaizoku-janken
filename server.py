"""
海賊じゃんけん 対人対戦サーバー（FastAPI + WebSocket）

機能:
- 部屋コードで友達と対戦 / 自動マッチ（ランダムな相手と対戦）
- 進行と判定はサーバー側。2人のブラウザに同じ結果を配信
- 再接続対応（スマホで別アプリに移って切れても、少しの間なら復帰）
- ゲーム中のチャット（吹き出し）中継
- 成績・強さはクライアント（ブラウザ）保存。join時に受け取り、結果を返す

起動:  uvicorn server:app --host 0.0.0.0 --port 8000
Render: uvicorn server:app --host 0.0.0.0 --port $PORT
"""
import asyncio
import json
import random
import string
import uuid
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()
BASE = Path(__file__).parent
IMG_DIR = BASE / "images"
IMG_DIR.mkdir(exist_ok=True)
app.mount("/img", StaticFiles(directory=str(IMG_DIR)), name="img")

BEATS = {"goo": "choki", "choki": "paa", "paa": "goo"}
TOTAL_ROUNDS = 5
PICK_SECONDS = 20
DISCONNECT_GRACE = 60


def make_deck():
    types = ["goo", "choki", "paa", "goo", "choki", "paa"]
    deck = [{"type": t, "star": False, "used": False} for t in types]
    deck[random.randrange(6)]["star"] = True
    return deck


def judge(a, b):
    if a == b:
        return "draw"
    return "win" if BEATS[a] == b else "lose"


class Player:
    def __init__(self, ws, name):
        self.ws = ws
        self.name = (name or "海賊").strip()[:12] or "海賊"
        self.token = uuid.uuid4().hex
        self.color = "#2f6d86"
        self.char = "1"
        self.room = None
        self.deck = make_deck()
        self.star = 0
        self.strength = 1500
        self.games = 0
        self.mwins = 0       # 通算 試合勝ち（クライアント保存分＋今セッション）
        self.mlosses = 0     # 通算 試合負け
        self.ready = False
        self.ready_next = False
        self.rematch = False
        self.pick = None
        self.timeouts = 0
        self.force_lose = False
        self.delta = 0
        self.last_result = None
        self.last_delta = 0
        self.connected = True
        self.disc_task = None

    async def send(self, obj):
        try:
            await self.ws.send_text(json.dumps(obj, ensure_ascii=False))
        except Exception:
            pass


def apply_profile(p, msg):
    def to_int(v, d):
        try:
            return int(v)
        except Exception:
            return d
    p.strength = to_int(msg.get("strength", 1500), 1500)
    p.games = to_int(msg.get("games", 0), 0)
    p.mwins = to_int(msg.get("mwins", 0), 0)
    p.mlosses = to_int(msg.get("mlosses", 0), 0)
    c = str(msg.get("color", ""))
    if c.startswith("#") and 4 <= len(c) <= 9:
        p.color = c
    ch = str(msg.get("char", "1"))
    if ch.isalnum() and len(ch) <= 4:
        p.char = ch


class Room:
    def __init__(self, code):
        self.code = code
        self.players = []
        self.phase = "lobby"
        self.round = 1
        self.first_round = True
        self.timer = None
        self.paused = False

    def opp(self, p):
        for q in self.players:
            if q is not p:
                return q
        return None


rooms = {}
matchmaking = []   # 自動マッチ待ちのPlayer


def gen_code():
    while True:
        c = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
        if c not in rooms:
            return c


def view(p):
    return {
        "name": p.name, "color": p.color, "char": p.char, "deck": p.deck, "star": p.star,
        "strength": p.strength, "games": p.games, "mwins": p.mwins, "mlosses": p.mlosses,
        "ready": p.ready, "readyNext": p.ready_next, "connected": p.connected,
    }


async def send_state(room):
    for p in room.players:
        o = room.opp(p)
        await p.send({
            "t": "state", "phase": room.phase, "round": room.round,
            "code": room.code, "players": len(room.players), "paused": room.paused,
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
    if room.phase != "playing" or room.round != rnd or room.paused:
        return
    for p in room.players:
        if p.pick is None:
            p.timeouts += 1
            remain = [i for i, c in enumerate(p.deck) if not c["used"]]
            if remain:
                p.pick = random.choice(remain)
            if p.timeouts >= 2:
                p.force_lose = True
    await try_resolve(room)


# ===== 進行 =====
async def start_match(room):
    for p in room.players:
        p.deck = make_deck()
        p.star = 0
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
    room.paused = False
    await send_state(room)
    for p in room.players:
        await p.send({"t": "round_start", "round": room.round, "first": room.first_round})
    room.first_round = False
    start_timer(room)


async def on_pick(room, p, idx):
    if room.phase != "playing" or room.paused or p.pick is not None:
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
    if wc["star"] and lc["star"]:
        winner.star += 3
    elif wc["star"]:
        winner.star += 2
    else:
        winner.star += 1


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
        res = judge(ac["type"], bc["type"])

    if res == "win":
        apply_score(a, ac, bc)
    elif res == "lose":
        apply_score(b, bc, ac)

    room.phase = "reveal"
    await send_state(room)

    inv = {"win": "lose", "lose": "win", "draw": "draw"}
    await a.send({"t": "reveal",
                  "you": {"type": ac["type"], "star": ac["star"]},
                  "opp": {"type": bc["type"], "star": bc["star"]}, "result": res})
    await b.send({"t": "reveal",
                  "you": {"type": bc["type"], "star": bc["star"]},
                  "opp": {"type": ac["type"], "star": ac["star"]}, "result": inv[res]})


async def on_ready_next(room, p):
    if room.phase != "reveal" or room.paused:
        return
    p.ready_next = True
    if len(room.players) == 2 and all(pl.ready_next for pl in room.players):
        await advance(room)


async def advance(room):
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
        if r == "win":
            p.mwins += 1
        elif r == "lose":
            p.mlosses += 1
        p.last_result = r
        p.last_delta = p.delta
    room.phase = "over"
    room.paused = False
    cancel_timer(room)
    await send_state(room)
    for p, r in [(a, ra), (b, rb)]:
        o = room.opp(p)
        await p.send({"t": "game_over", "result": r, "yourStar": p.star,
                      "oppStar": (o.star if o else 0), "delta": p.delta})


async def on_ready(room, p):
    if room.phase != "ready" or room.paused:
        return
    p.ready = True
    await send_state(room)
    if len(room.players) == 2 and all(pl.ready for pl in room.players):
        await start_match(room)


async def on_rematch(room, p):
    if room.phase != "over" or room.paused:
        return
    p.rematch = True
    if len(room.players) == 2 and all(pl.rematch for pl in room.players):
        for pl in room.players:
            pl.ready = False
            pl.rematch = False
        room.phase = "ready"
        await send_state(room)


async def on_chat(room, p, text):
    txt = (text or "").strip()[:40]
    if not txt:
        return
    await p.send({"t": "chat", "side": "you", "text": txt})
    o = room.opp(p)
    if o:
        await o.send({"t": "chat", "side": "opp", "text": txt})


# ===== 部屋作成/参加・自動マッチ =====
async def join_room(ws, player, code):
    if not code:
        code = gen_code()
    room = rooms.get(code)
    if room is None:
        room = Room(code)
        rooms[code] = room
    if len(room.players) >= 2:
        await ws.send_text(json.dumps({"t": "error", "msg": "その部屋は満員です"}, ensure_ascii=False))
        return None
    room.players.append(player)
    player.room = room
    await player.send({"t": "joined", "you": room.players.index(player),
                       "code": room.code, "token": player.token})
    if len(room.players) == 2 and room.phase == "lobby":
        room.phase = "ready"
    await send_state(room)
    return room


async def quick_match(player):
    partner = None
    while matchmaking:
        cand = matchmaking.pop(0)
        if cand.connected and cand.room is None:
            partner = cand
            break
    if partner:
        room = Room(gen_code())
        rooms[room.code] = room
        room.players = [partner, player]
        partner.room = room
        player.room = room
        room.phase = "ready"
        for pl, idx in ((partner, 0), (player, 1)):
            await pl.send({"t": "joined", "you": idx, "code": room.code, "token": pl.token})
        await send_state(room)
    else:
        matchmaking.append(player)
        await player.send({"t": "searching"})


# ===== 再接続 =====
async def reattach(room, player, ws):
    if player.disc_task and not player.disc_task.done():
        player.disc_task.cancel()
    player.disc_task = None
    player.ws = ws
    player.connected = True
    room.paused = False
    await player.send({"t": "joined", "you": room.players.index(player),
                       "code": room.code, "token": player.token})
    o = room.opp(player)
    if o:
        await o.send({"t": "opp_resumed"})
    await resync(room, player)


async def resync(room, player):
    await send_state(room)
    if room.phase == "playing":
        await start_round(room)
    elif room.phase == "reveal":
        for p in room.players:
            p.ready_next = True
        await advance(room)
    elif room.phase == "over":
        o = room.opp(player)
        await player.send({"t": "game_over", "result": player.last_result or "draw",
                           "yourStar": player.star, "oppStar": (o.star if o else 0),
                           "delta": player.last_delta})


async def disc_grace(room, player):
    try:
        await asyncio.sleep(DISCONNECT_GRACE)
    except asyncio.CancelledError:
        return
    if player.connected:
        return
    if player in room.players:
        room.players.remove(player)
    room.paused = False
    o = room.opp(player)
    if o:
        await o.send({"t": "opp_left"})
        room.phase = "lobby"
        await send_state(room)
    if not room.players:
        rooms.pop(room.code, None)


async def handle_disconnect(player):
    if not player:
        return
    player.connected = False
    if player in matchmaking:
        try:
            matchmaking.remove(player)
        except ValueError:
            pass
        return
    room = player.room
    if not room:
        return
    if room.phase in ("ready", "playing", "reveal") and len(room.players) == 2:
        cancel_timer(room)
        room.paused = True
        o = room.opp(player)
        if o:
            await o.send({"t": "opp_paused"})
        player.disc_task = asyncio.create_task(disc_grace(room, player))
    else:
        cancel_timer(room)
        if player in room.players:
            room.players.remove(player)
        o = room.opp(player)
        if o:
            await o.send({"t": "opp_left"})
            room.phase = "lobby"
            room.paused = False
            await send_state(room)
        if not room.players:
            rooms.pop(room.code, None)


# ===== ルーティング =====
@app.get("/")
async def index():
    return FileResponse(BASE / "index.html")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    player = None
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            t = msg.get("t")
            room = player.room if player else None

            if t == "join" and player is None:
                code = (msg.get("room") or "").strip().upper()
                token = msg.get("token")
                if token and code and code in rooms:
                    p = next((x for x in rooms[code].players
                              if x.token == token and not x.connected), None)
                    if p:
                        player = p
                        await reattach(rooms[code], player, ws)
                        continue
                player = Player(ws, msg.get("name", ""))
                apply_profile(player, msg)
                await join_room(ws, player, code)

            elif t == "quickmatch" and player is None:
                player = Player(ws, msg.get("name", ""))
                apply_profile(player, msg)
                await quick_match(player)

            elif t == "ping":
                if player:
                    await player.send({"t": "pong"})
            elif player is None:
                continue
            elif t == "cancel_match":
                if player in matchmaking:
                    matchmaking.remove(player)
                await player.send({"t": "match_cancelled"})
            elif room is None:
                continue
            elif t == "chat":
                await on_chat(room, player, msg.get("text"))
            elif room.paused:
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
        await handle_disconnect(player)
