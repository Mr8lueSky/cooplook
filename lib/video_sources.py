import abc
import os
from pathlib import Path
from typing import override
from uuid import UUID, uuid1

from fastapi import Request, Response
from fastapi.responses import RedirectResponse

import config
from lib.custom_responses import LoadingTorrentFileResponse
from lib.torrent.torrent_info import TorrentInfo
from models.room_model import RoomModel, VideoSourcesEnum
from lib.torrent.torrent_handler import FileTorrentHandler


class VideoSource(abc.ABC):
    data_field: str
    enum: VideoSourcesEnum

    def __init__(self, data: str, file_index: int) -> None:
        super().__init__()
        self.file_index: int = file_index

    @abc.abstractmethod
    def get_available_files(self) -> list[tuple[int, str]]: ...

    @abc.abstractmethod
    def set_file_index(self, fi: int) -> bool: ...

    def start(self): ...

    @abc.abstractmethod
    def cancel_current_requests(self): ...

    def cleanup(self):
        self.cancel_current_requests()

    @classmethod
    def from_model(cls, model: RoomModel) -> "VideoSource":
        cls = enum_to_source.get(model.video_source)
        if cls is None:
            raise RuntimeError(f"Unknown source: {cls}")
        return cls(model.video_source_data, model.last_file_ind)

    def update_model(self, model: RoomModel) -> RoomModel:
        model.video_source = self.enum
        model.video_source_data = getattr(self, self.data_field)
        return model

    @abc.abstractmethod
    async def get_video_response(self, request: Request) -> Response: ...


class HttpLinkVideoSource(VideoSource):
    data_field: str = "link"
    enum: VideoSourcesEnum = VideoSourcesEnum.link

    def __init__(self, link: str, file_index: int) -> None:
        super().__init__(link, file_index)
        self.link: str = link

    @override
    def get_available_files(self) -> list[tuple[int, str]]:
        return [(0, self.link)]

    @override
    def set_file_index(self, fi: int) -> bool:
        return False

    @override
    def cancel_current_requests(self): ...

    @override
    async def get_video_response(self, request: Request) -> RedirectResponse:
        return RedirectResponse(self.link, 303)


class SortedToTorrentFileIndex:
    def __init__(self, torrent: TorrentInfo) -> None:
        files: list[tuple[int, str]] = [
            (i, torrent.get_file_name(i)) for i in range(torrent.files_count())
        ]
        self.sorted: list[tuple[int, str]] = sorted(files, key=lambda x: x[1])

    def get_sorted(self) -> list[tuple[int, str]]:
        return [(i, filename) for i, (_, filename) in enumerate(self.sorted)]

    def sorted_to_original(self, ind: int) -> int:
        return self.sorted[ind][0]


class TorrentVideoSource(VideoSource):
    SAVE_PATH: Path = config.TORRENT_SAVE_PATH
    data_field: str = "torrent_path"
    enum: VideoSourcesEnum = VideoSourcesEnum.torrent

    def __init__(
        self,
        torrent_path: str,
        file_index: int,
    ):
        super().__init__("", file_index)
        self.folder_id: UUID = uuid1()
        os.makedirs(self.save_path, exist_ok=True)
        self.torrent_path: str = torrent_path
        self.torrent: TorrentInfo = TorrentInfo(self.torrent_path, self.save_path)
        self.torrent_manager: FileTorrentHandler = FileTorrentHandler(
            self.torrent, self.file_index
        )
        self.resps: list[LoadingTorrentFileResponse] = []
        self.file_mapping: SortedToTorrentFileIndex = SortedToTorrentFileIndex(
            self.torrent
        )
        self.file_index = -1
        _ = self.set_file_index(file_index)

    @property
    def save_path(self) -> str:
        return str(self.SAVE_PATH / str(self.folder_id))

    @override
    def set_file_index(self, fi: int) -> bool:
        """Returns is file changed or not"""
        if fi == self.file_index:
            return False
        torrent_ind = self.file_mapping.sorted_to_original(fi)
        self.torrent_manager.set_file_index(torrent_ind)
        self.file_index: int = fi
        return True

    @override
    def cleanup(self):
        super().cleanup()
        self.cancel_current_requests()
        self.torrent_manager.cleanup()

    @override
    def start(self):
        os.makedirs(self.save_path, exist_ok=True)

    @override
    def cancel_current_requests(self):
        for r in self.resps:
            r.cancel()
        self.resps.clear()

    @override
    def get_available_files(self) -> list[tuple[int, str]]:
        return self.file_mapping.get_sorted()

    @override
    async def get_video_response(self, request: Request) -> LoadingTorrentFileResponse:
        _ = await self.torrent_manager.wait_file_ready()
        r = LoadingTorrentFileResponse(self.torrent_manager, request)
        self.resps.append(r)
        return r


enum_to_source: dict[VideoSourcesEnum, type[VideoSource]] = {
    VideoSourcesEnum.torrent: TorrentVideoSource,
    VideoSourcesEnum.link: HttpLinkVideoSource,
}
