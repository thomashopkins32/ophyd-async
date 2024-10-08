import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from urllib.parse import urlunparse

from bluesky.protocols import Hints, StreamAsset
from event_model import (
    ComposeStreamResource,
    DataKey,
    StreamRange,
)

from ophyd_async.core._detector import DetectorWriter
from ophyd_async.core._providers import DatasetDescriber, NameProvider, PathProvider
from ophyd_async.core._signal import (
    observe_value,
    set_and_wait_for_value,
    wait_for_value,
)
from ophyd_async.core._status import AsyncStatus
from ophyd_async.core._utils import DEFAULT_TIMEOUT

from ._core_io import NDArrayBaseIO, NDFileIO
from ._utils import FileWriteMode


class ADWriter(DetectorWriter):
    def __init__(
        self,
        fileio: NDFileIO,
        path_provider: PathProvider,
        name_provider: NameProvider,
        dataset_describer: DatasetDescriber,
        *plugins: NDArrayBaseIO,
        file_extension: str = ".tiff",
        mimetype: str = "multipart/related;type=image/tiff",
    ) -> None:
        self.fileio = fileio
        self._path_provider = path_provider
        self._name_provider = name_provider
        self._dataset_describer = dataset_describer
        self._file_extension = file_extension
        self._mimetype = mimetype
        self._last_emitted = 0
        self._emitted_resource = None

        self._plugins = plugins
        self._capture_status: AsyncStatus | None = None
        self._multiplier = 1
        self._filename_template = "%s%s_%6.6d"
        self._auto_increment_file_counter = True

    async def begin_capture(self) -> None:
        info = self._path_provider(device_name=self._name_provider())

        await self.fileio.enable_callbacks.set(True)

        # Set the directory creation depth first, since dir creation callback happens
        # when directory path PV is processed.
        await self.fileio.create_directory.set(info.create_dir_depth)

        await asyncio.gather(
            # See https://github.com/bluesky/ophyd-async/issues/122
            self.fileio.file_path.set(str(info.directory_path)),
            self.fileio.file_name.set(info.filename),
            self.fileio.file_template.set(
                self._filename_template + self._file_extension
            ),
            self.fileio.file_write_mode.set(FileWriteMode.stream),
            self.fileio.auto_increment.set(True),
        )

        assert (
            await self.fileio.file_path_exists.get_value()
        ), f"File path {info.directory_path} for file plugin does not exist!"

        # Overwrite num_capture to go forever
        await self.fileio.num_capture.set(0)
        # Wait for it to start, stashing the status that tells us when it finishes
        self._capture_status = await set_and_wait_for_value(self.fileio.capture, True)

    async def open(self, multiplier: int = 1) -> dict[str, DataKey]:
        self._emitted_resource = None
        self._last_emitted = 0
        frame_shape = await self._dataset_describer.shape()
        dtype_numpy = await self._dataset_describer.np_datatype()

        await self.begin_capture()

        describe = {
            self._name_provider(): DataKey(
                source=self._name_provider(),
                shape=frame_shape,
                dtype="array",
                dtype_numpy=dtype_numpy,
                external="STREAM:",
            )  # type: ignore
        }
        return describe

    async def observe_indices_written(
        self, timeout=DEFAULT_TIMEOUT
    ) -> AsyncGenerator[int, None]:
        """Wait until a specific index is ready to be collected"""
        async for num_captured in observe_value(self.fileio.num_captured, timeout):
            yield num_captured // self._multiplier

    async def get_indices_written(self) -> int:
        num_captured = await self.fileio.num_captured.get_value()
        return num_captured // self._multiplier

    async def collect_stream_docs(
        self, indices_written: int
    ) -> AsyncIterator[StreamAsset]:
        if indices_written:
            if not self._emitted_resource:
                file_path = Path(await self.fileio.file_path.get_value())
                file_name = await self.fileio.file_name.get_value()
                file_template = file_name + "_{:06d}" + self._file_extension

                frame_shape = await self._dataset_describer.shape()

                uri = urlunparse(
                    (
                        "file",
                        "localhost",
                        str(file_path.absolute()) + "/",
                        "",
                        "",
                        None,
                    )
                )

                bundler_composer = ComposeStreamResource()

                self._emitted_resource = bundler_composer(
                    mimetype=self._mimetype,
                    uri=uri,
                    data_key=self._name_provider(),
                    parameters={
                        "chunk_shape": (1, *frame_shape),
                        "template": file_template,
                    },
                    uid=None,
                    validate=True,
                )

                yield "stream_resource", self._emitted_resource.stream_resource_doc

            # Indices are relative to resource
            if indices_written > self._last_emitted:
                indices: StreamRange = {
                    "start": self._last_emitted,
                    "stop": indices_written,
                }
                self._last_emitted = indices_written
                yield (
                    "stream_datum",
                    self._emitted_resource.compose_stream_datum(indices),
                )

    async def close(self):
        # Already done a caput callback in _capture_status, so can't do one here
        await self.fileio.capture.set(False, wait=False)
        await wait_for_value(self.fileio.capture, False, DEFAULT_TIMEOUT)
        if self._capture_status:
            # We kicked off an open, so wait for it to return
            await self._capture_status

    @property
    def hints(self) -> Hints:
        return {"fields": [self._name_provider()]}
