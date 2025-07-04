import asyncio
from asyncio import Lock
from dataclasses import dataclass, field
from time import time
from traceback import format_exception
from uuid import UUID

from fastapi import WebSocket
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocketDisconnect

from cmds import Commands, VideoStatus
from config import ROOM_INACTIVITY_PERIOD
from engine import async_session_maker
from logger import Logging, create_logger
from models.room_model import RoomModel, enum_to_source
from video_sources import VideoSource

MOE = 1
rooms: dict[UUID, "RoomInfo"] = {}
lock = Lock()
monitor_logger = create_logger("RoomMonitor")

cmd_data_prepare = {
    Commands.PAUSE: float,
    Commands.PLAY: float,
    Commands.SUSPEND: float,
    Commands.UNSUSPEND: float,
    Commands.CHANGE_FILE: int,
}


@dataclass
class RoomInfo(Logging):
    video_source: VideoSource
    name: str
    room_id: UUID
    img_link: str
    unsuspend_to: VideoStatus = VideoStatus.PLAY
    wss: dict[int, WebSocket] = field(default_factory=dict)
    last_ws_id: int = 0
    _current_time: float = 0
    status: VideoStatus = VideoStatus.PAUSE
    last_change: float = field(default_factory=time)
    suspend_by: set[int] = field(default_factory=set)
    last_leave_ts: float = field(default_factory=time)

    @classmethod
    def from_model(cls, model: RoomModel) -> "RoomInfo":
        vs_cls: type[VideoSource] = enum_to_source[model.video_source]
        r = RoomInfo(
            vs_cls(model.video_source_data, model.last_file_ind, model.room_id),
            model.name,
            model.room_id,
            model.img_link,
            _current_time=model.last_watch_ts,
        )
        r.video_source.start()
        return r

    @property
    def curr_fi(self):
        return self.video_source.curr_fi

    @property
    def video(self):
        return self.video_source.get_player_src()

    async def handle_set_new_file(self, new_fi: int, ws_id: int) -> bool:
        if not self.video_source.set_current_fi(new_fi):
            return False
        await self.send_room(Commands.change_file_cmd(new_fi), ws_id)
        await self.handle_play_pause(Commands.PAUSE, 0, -1)
        asyncio.gather(
            *(self.initialize_user(ws, ws_id) for ws_id, ws in self.wss.items())
        )
        return True

    def get_available_files(self):
        return self.video_source.get_available_files()

    async def send_to(self, msg: str, to: int):
        try:
            if to in self.wss:
                await self.wss[to].send_text(msg)
        except RuntimeError:
            await self.handle_leave(to)

    async def send_room(self, msg: str, by: int = -1):
        self.logger.debug(f"Sn room {msg}, {by}")
        await asyncio.gather(
            *(self.send_to(msg, ws_id) for ws_id in self.wss.keys() if ws_id != by)
        )

    async def change_status(self, new_status: VideoStatus, ts: float, by: int):
        self.logger.info(f"Changing status to {new_status}")
        self.current_time = ts
        self.last_change = time()

        if abs(self.current_time - ts) > MOE:
            self.status = VideoStatus.SUSPEND
            await self.send_current_status(-1)
        else:
            self.status = new_status
            await self.send_current_status(by)

    async def send_current_status(self, by: int):
        await self.send_room(f"{self.status.value} {self.current_time}", by)

    async def handle_play_pause(self, cmd: Commands, ts: float, by: int):
        if self.status == VideoStatus.SUSPEND:
            return
        await self.change_status(VideoStatus(cmd), ts, by)

    async def handle_susp_unsusp(self, cmd: Commands, ts: float, by: int):
        if cmd == Commands.SUSPEND:
            self.suspend_by.add(by)
            if not self.status == VideoStatus.SUSPEND:
                await self.change_status(VideoStatus.SUSPEND, ts, -1)

        if cmd == Commands.UNSUSPEND and by in self.suspend_by:
            self.suspend_by.remove(by)

        if not self.suspend_by and self.status == VideoStatus.SUSPEND:
            await self.send_room(Commands.unsuspend_cmd(self.current_time))
            await self.change_status(self.unsuspend_to, ts, -1)
            self.unsuspend_to = VideoStatus.PLAY

    async def suspend_by_all(self, ts: float):
        for i in self.wss.keys():
            await self.handle_susp_unsusp(Commands.SUSPEND, ts, i)

    async def handle_cmd(self, data: str, ws_id: int):
        self.logger.debug(f"Rc: {ws_id}, {data}")
        cmd, arg = data.split(" ")
        cmd = Commands(cmd)
        arg = cmd_data_prepare.get(cmd, str)(arg)
        if cmd == Commands.PLAY or cmd == Commands.PAUSE:
            await self.handle_play_pause(cmd, arg, ws_id)
        elif cmd == Commands.SUSPEND or cmd == Commands.UNSUSPEND:
            await self.handle_susp_unsusp(cmd, arg, ws_id)
        elif cmd == Commands.CHANGE_FILE:
            await self.handle_set_new_file(arg, ws_id)
        async with async_session_maker.begin() as session:
            await RoomModel.update(
                session, self.room_id, self.current_time, self.video_source.curr_fi
            )

    async def handle_leave(self, ws_id: int):
        self.wss.pop(ws_id, None)
        if ws_id in self.suspend_by:
            await self.handle_susp_unsusp(Commands.UNSUSPEND, self.current_time, ws_id)
        await self.handle_play_pause(Commands.PAUSE, self.current_time, -1)
        await self.send_room(Commands.people_count_cmd(len(self.wss)))
        self.last_leave_ts = time()

    async def handle_client(self, websocket: WebSocket):
        ws_id = self.last_ws_id
        self.last_ws_id += 1
        try:
            await self.initialize_user(websocket, ws_id)
            self.logger.info(f"Client {ws_id} connected")
            while True:
                data = await websocket.receive_text()
                await self.handle_cmd(data, ws_id)
        except WebSocketDisconnect:
            self.logger.info(f"User {ws_id} disconnected")
        except Exception as exc:
            self.logger.error("\n".join(format_exception(exc)))
        await self.handle_leave(ws_id)

    @property
    def current_time(self):
        if self.status == VideoStatus.PLAY:
            return self._current_time + time() - self.last_change
        return self._current_time

    @current_time.setter
    def current_time(self, ts: float):
        self.logger.info(f"Set current time from {self._current_time} to {ts}")
        self._current_time = ts
        self.last_change = time()

    async def initialize_user(self, ws: WebSocket, ws_id: int):
        self.wss[ws_id] = ws
        await ws.accept()
        self.unsuspend_to = VideoStatus.PAUSE
        await self.handle_susp_unsusp(Commands.SUSPEND, self.current_time, ws_id)
        await self.send_room(Commands.people_count_cmd(len(self.wss)), -1)

    async def cleanup(self):
        self.video_source.cancel_current_requests()
        self.video_source.cleanup()

    @property
    def files(self):
        return self.get_available_files()


async def retake_room(session: AsyncSession, room_id: UUID):
    prev_room = rooms.pop(room_id, None)
    if prev_room:
        await prev_room.cleanup()
    room_model = await RoomModel.get_room_id(session, room_id)
    rooms[room_id] = RoomInfo.from_model(room_model)


async def get_room(session: AsyncSession, room_id: UUID) -> RoomInfo:
    async with lock:
        if room_id not in rooms:
            await retake_room(session, room_id)
    return rooms[room_id]


async def delete_room(session: AsyncSession, room_id: UUID):
    room = await get_room(session, room_id)
    rooms.pop(room_id)
    await RoomModel.delete(session, room_id)
    await room.cleanup()


async def _monitor_rooms():
    while True:
        await asyncio.sleep(60)
        monitor_logger.info("Starting room cleanup")
        for room_id, room in tuple(rooms.items()):
            async with lock:
                if (
                    len(room.wss)
                    or time() - room.last_leave_ts < ROOM_INACTIVITY_PERIOD
                ):
                    monitor_logger.debug(
                        f"Skipping room {room_id}. "
                        f"Time from last leave: {time() - room.last_leave_ts}, "
                        f"people count: {len(room.wss)}"
                    )
                    continue
                room = rooms.pop(room_id)
            await room.cleanup()
            monitor_logger.debug(f"Cleaned room {room_id}")
            await asyncio.sleep(0)
        monitor_logger.info("Room cleanup finished")


async def monitor_rooms():
    asyncio.create_task(_monitor_rooms())
