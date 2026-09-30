"""Minimal AstrBot host and in-memory E2B service for lifecycle regression tests."""

import asyncio
import importlib.util
import logging
import shutil
import sys
import types
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class TestDirectory:
    def __init__(self):
        self.root = (Path(__file__).resolve().parents[1] / ".test-tmp").resolve()
        self.path = self.root / ("case-" + uuid.uuid4().hex)
        self.path.mkdir(parents=True)
        self.name = str(self.path)

    def cleanup(self):
        if self.path.resolve().parent != self.root:
            raise RuntimeError("Test cleanup escaped its workspace directory")
        shutil.rmtree(self.path)


def load_main():
    root = Path(__file__).resolve().parents[1]
    for name in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.message_components", "astrbot.api.provider"):
        sys.modules.setdefault(name, types.ModuleType(name))
    api = sys.modules["astrbot.api"]
    class Star:
        def __init__(self, context):
            self.context = context
    api.star = types.SimpleNamespace(Star=Star, Context=object)
    api.FunctionTool = type("FunctionTool", (), {})
    api.logger = logging.getLogger("e2b-test")
    api.logger.addHandler(logging.NullHandler())
    events = sys.modules["astrbot.api.event"]
    events.AstrMessageEvent = object
    events.filter = types.SimpleNamespace(
        event_message_type=lambda *_: lambda f: f,
        on_llm_request=lambda: lambda f: f,
        EventMessageType=types.SimpleNamespace(ALL=0),
    )
    components = sys.modules["astrbot.api.message_components"]
    components.Image = type("Image", (), {})
    components.File = type("File", (), {"__init__": lambda self, **kw: self.__dict__.update(kw)})
    sys.modules["astrbot.api.provider"].ProviderRequest = object
    package = types.ModuleType("sandbox_plugin_test")
    package.__path__ = [str(root)]
    sys.modules[package.__name__] = package
    spec = importlib.util.spec_from_file_location("sandbox_plugin_test.main", root / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class State(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"


@dataclass
class Query:
    metadata: dict = None
    state: list = None


class NotFoundException(Exception):
    pass


class FakeSandbox:
    def __init__(self, sandbox_id, metadata):
        self.sandbox_id = sandbox_id
        self.metadata = metadata
        self.state = State.RUNNING
        self.commands = types.SimpleNamespace(run=self.command)
        self.files = types.SimpleNamespace(write=self.write)
        self.run_started = asyncio.Event()
        self.run_gate = None
        self.run_error = None

    async def command(self, *args, **kwargs):
        return types.SimpleNamespace(exit_code=0, stdout="", stderr="")

    async def write(self, *args, **kwargs):
        pass

    async def run_code(self, code, **kwargs):
        self.run_started.set()
        if self.run_gate:
            await self.run_gate.wait()
        if self.run_error:
            raise self.run_error
        kwargs["on_stdout"](types.SimpleNamespace(line="ok\n"))
        return types.SimpleNamespace(results=[], text="", error=None)

    async def pause(self):
        return await FakeSDK.pause(self.sandbox_id)


class FakeSDK:
    @classmethod
    def reset(cls):
        cls.boxes = {}
        cls.create_count = cls.connect_count = cls.pause_count = cls.kill_count = 0
        cls.peak = 0
        cls.pause_error = cls.kill_error = cls.create_error = None
        cls.create_gate = None

    @classmethod
    def count(cls):
        running = sum(b.state == State.RUNNING for b in cls.boxes.values())
        cls.peak = max(cls.peak, running)
        return running

    @classmethod
    async def create(cls, template=None, timeout=None, metadata=None, lifecycle=None, **opts):
        cls.create_count += 1
        if cls.create_error:
            raise cls.create_error
        box = FakeSandbox("sbx-" + str(cls.create_count), metadata or {})
        cls.boxes[box.sandbox_id] = box
        cls.count()
        if cls.create_gate:
            await cls.create_gate.wait()
        return box

    @classmethod
    async def connect(cls, sandbox_id, **opts):
        cls.connect_count += 1
        if sandbox_id not in cls.boxes:
            raise NotFoundException("404: Sandbox not found")
        box = cls.boxes[sandbox_id]
        box.state = State.RUNNING
        cls.count()
        return box

    @classmethod
    async def pause(cls, sandbox_id, **opts):
        cls.pause_count += 1
        if cls.pause_error:
            raise cls.pause_error
        if sandbox_id not in cls.boxes:
            raise NotFoundException("404: Sandbox not found")
        cls.boxes[sandbox_id].state = State.PAUSED
        return True

    @classmethod
    async def kill(cls, sandbox_id, **opts):
        cls.kill_count += 1
        if cls.kill_error:
            raise cls.kill_error
        return cls.boxes.pop(sandbox_id, None) is not None

    @classmethod
    async def get_info(cls, sandbox_id, **opts):
        if sandbox_id not in cls.boxes:
            raise NotFoundException("404: Sandbox not found")
        return cls.boxes[sandbox_id]

    @classmethod
    def list(cls, query, **opts):
        assert isinstance(query, Query)
        class Paginator:
            has_next = True
            async def next_items(self):
                self.has_next = False
                return [
                    box for box in cls.boxes.values()
                    if all(box.metadata.get(k) == v for k, v in query.metadata.items())
                    and box.state in query.state
                ]
        return Paginator()


class Event:
    def __init__(self, user="user"):
        self.unified_msg_origin = "test:friend:" + user
        self.user = user
        self.message_obj = types.SimpleNamespace(message=[], raw_message=None)
        self.sent = []
    def get_sender_id(self):
        return self.user
    def chain_result(self, chain):
        return chain
    async def send(self, result):
        self.sent.append(result)
