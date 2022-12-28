import json
import uuid
from enum import Enum, IntEnum
from typing import Optional

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import NoResultFound

from .db import engine

MAX_ROOM_USER_COUNT = 4
LIVE_ID_NULL = 0


class InvalidToken(Exception):
    """指定されたtokenが不正だったときに投げる"""


class InvalidId(Exception):
    """指定されたIdが不正だったときに投げる"""


class SafeUser(BaseModel):
    """token を含まないUser"""

    id: int
    name: str
    leader_card_id: int

    class Config:
        orm_mode = True


def create_user(name: str, leader_card_id: int) -> str:
    """Create new user and returns their token"""
    token = str(uuid.uuid4())
    # NOTE: tokenが衝突したらリトライする必要がある.
    # TODO: エラー時リトライ
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "INSERT INTO `user` (name, token, leader_card_id) VALUES (:name, :token, :leader_card_id)"
            ),
            {"name": name, "token": token, "leader_card_id": leader_card_id},
        )
        # print(f"create_user(): id={result.lastrowid} {token=}")
    return token


def _get_user_by_token(conn, token: str) -> Optional[SafeUser]:
    # SELECT * FROM `user` WHERE `token`={token}
    result = conn.execute(
        text("SELECT `id`, `name`, `leader_card_id` FROM `user` WHERE `token`=:token"),
        {"token": token},
    )
    try:
        row = result.one()
    except NoResultFound:
        return None
    return SafeUser.from_orm(row)


def get_user_by_token(token: str) -> Optional[SafeUser]:
    with engine.begin() as conn:
        return _get_user_by_token(conn, token)


def _update_user(conn, token: str, name: str, leader_card_id: int) -> None:
    # UPDATE `user` SET name={name}, leader_card_id={leader_card_id} WHERE token={token}
    conn.execute(
        text(
            "UPDATE `user` SET `name`=:name, `leader_card_id`=:leader_card_id WHERE `token`=:token"
        ),
        {"name": name, "token": token, "leader_card_id": leader_card_id},
    )


def update_user(token: str, name: str, leader_card_id: int) -> None:
    with engine.begin() as conn:
        _update_user(conn=conn, token=token, name=name, leader_card_id=leader_card_id)


class LiveDifficulty(IntEnum):
    Normal = 1
    Hard = 2


def _create_room(
    conn, token: str, live_id: int, select_difficulty: LiveDifficulty
) -> Optional[int]:
    user = _get_user_by_token(conn=conn, token=token)
    if user is None:
        raise InvalidToken

    # roomテーブルに部屋追加
    result = conn.execute(
        text(
            "INSERT INTO `room` SET `live_id`=:live_id, `joined_user_count`=1, `max_user_count`=:max_user_count"
        ),
        {"live_id": live_id, "max_user_count": MAX_ROOM_USER_COUNT},
    )

    room_id = result.lastrowid
    user_id = user.id

    # room_userテーブルにユーザー追加
    result = conn.execute(
        text(
            "INSERT INTO `room_user` SET `room_id`=:room_id, `user_id`=:user_id, `select_difficulty`=:select_difficulty, `is_host`=true"
        ),
        {
            "room_id": room_id,
            "user_id": user_id,
            "select_difficulty": int(select_difficulty),
        },
    )
    return room_id


def create_room(
    token: str, live_id: int, select_difficulty: LiveDifficulty
) -> Optional[int]:
    with engine.begin() as conn:
        return _create_room(
            conn=conn, token=token, live_id=live_id, select_difficulty=select_difficulty
        )


class RoomInfo(BaseModel):
    room_id: int
    live_id: int
    joined_user_count: int
    max_user_count: int

    class Config:
        orm_mode = True


def _list_room(conn, live_id: int) -> list[RoomInfo]:
    """ルーム一覧を取得 live_id=LIVE_ID_NULLで全部屋"""
    if live_id == LIVE_ID_NULL:
        result = conn.execute(
            text(
                "SELECT `room_id`, `live_id`, `joined_user_count`, `max_user_count` FROM `room` WHERE `joined_user_count` < `max_user_count` AND `is_playing`=false"
            )
        )
    else:
        result = conn.execute(
            text(
                "SELECT `room_id`, `live_id`, `joined_user_count`, `max_user_count` FROM `room` WHERE `joined_user_count` < `max_user_count` AND live_id=:live_id AND `is_playing`=false"
            ),
            {"live_id": live_id},
        )

    rows = result.fetchall()
    room_list = []
    for _, row in enumerate(rows):
        room_list.append(RoomInfo.from_orm(row))
    return room_list


def list_room(live_id: int) -> list[RoomInfo]:
    with engine.begin() as conn:
        return _list_room(conn, live_id=live_id)


class JoinRoomResult(IntEnum):
    Ok = 1
    RoomFull = 2
    Disbanded = 3  # 解散
    OtherError = 4


def _join_room(
    conn, token: str, room_id: int, select_difficulty: LiveDifficulty
) -> JoinRoomResult:
    user = _get_user_by_token(conn=conn, token=token)
    if user is None:
        raise InvalidToken

    # 空きがあるか確認
    result = conn.execute(
        text(
            "SELECT `joined_user_count`, `max_user_count` FROM `room` WHERE room_id=:room_id AND `is_playing`=false FOR UPDATE"
        ),
        {"room_id": room_id},
    )

    try:
        row = result.one()
    except NoResultFound:
        conn.rollback()
        return JoinRoomResult.Disbanded

    if row["joined_user_count"] >= row["max_user_count"]:
        conn.rollback()
        return JoinRoomResult.RoomFull

    # 部屋に追加
    result = conn.execute(
        text(
            "INSERT INTO `room_user` SET `room_id`=:room_id, `user_id`=:user_id, `select_difficulty`=:select_difficulty, `is_host`=false"
        ),
        {
            "room_id": room_id,
            "user_id": user.id,
            "select_difficulty": int(select_difficulty),
        },
    )

    # 多重joinチェック
    result = conn.execute(
        text("SELECT `user_id` FROM room_user WHERE `user_id`=:user_id"),
        {"user_id": user.id},
    )
    try:
        row = result.one()
    except NoResultFound:  # 2件以上もNoResultFound
        conn.rollback()
        return JoinRoomResult.OtherError

    conn.commit()
    return JoinRoomResult.Ok


def join_room(
    token: str, room_id: int, select_difficulty: LiveDifficulty
) -> JoinRoomResult:
    with engine.begin() as conn:
        return _join_room(
            conn, token, room_id=room_id, select_difficulty=select_difficulty
        )


class WaitRoomStatus(IntEnum):
    Waiting = 1
    LiveStart = 2
    Dissolution = 3  # 解散


class RoomUser(BaseModel):
    user_id: int
    name: str
    leader_card_id: int  # 設定アバター
    select_difficulty: LiveDifficulty
    is_me: bool  # リクエストを投げたユーザーと同じか
    is_host: bool  # 部屋を立てた人か

    class Config:
        orm_mode = True


class WaitRoomResult(BaseModel):
    status: WaitRoomStatus
    room_user_list: list[RoomUser]


def _wait_room(conn, token: str, room_id: int) -> WaitRoomResult:
    user = _get_user_by_token(conn=conn, token=token)
    if user is None:
        raise InvalidToken

    result = conn.execute(
        text("SELECT `is_playing` FROM `room` WHERE `room_id`=:room_id "),
        {"room_id": room_id},
    )
    try:
        row = result.one()
    except NoResultFound:
        conn.rollback()
        return WaitRoomResult(status=WaitRoomStatus.Dissolution, room_user_list=[])

    is_playing = row["is_playing"]

    result = conn.execute(
        text(
            "SELECT `user_id`, `select_difficulty`, `is_host` FROM `room_user` WHERE `room_id`=:room_id"
        ),
        {
            "room_id": room_id,
        },
    )
    rows = result.fetchall()

    room_user_list = []
    for _, row in enumerate(rows):
        is_me = row["user_id"] == user.id

        result = conn.execute(
            text("SELECT `name`, `leader_card_id` FROM `user` WHERE `id`=:user_id"),
            {
                "user_id": room_id,
            },
        )
        u = result.one()
        name = u["name"]
        leader_card_id = u["leader_card_id"]
        room_user_list.append(
            RoomUser(**row, name=name, leader_card_id=leader_card_id, is_me=is_me)
        )

    if is_playing:
        return WaitRoomResult(
            status=WaitRoomStatus.LiveStart, room_user_list=room_user_list
        )
    else:
        return WaitRoomResult(
            status=WaitRoomStatus.Waiting, room_user_list=room_user_list
        )


def wait_room(token: str, room_id: int) -> WaitRoomResult:
    with engine.begin() as conn:
        return _wait_room(conn, token, room_id)


# room/start
def _start_room(conn, room_id: int) -> None:
    result = conn.execute(
        text("UPDATE `room` SET `is_playing`=true WHERE `room_id`=:room_id"),
        {"room_id": room_id},
    )


def start_room(room_id: int) -> None:
    with engine.begin() as conn:
        _start_room(conn, room_id)


class ResultUser(BaseModel):
    user_id: int
    judge_count_list: list[int]  # 各判定数(良い判定から昇順)
    score: int

    class Config:
        orm_mode = True
