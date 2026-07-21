"""In-memory stream bridge backed by an in-process event log."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from .base import END_SENTINEL, HEARTBEAT_SENTINEL, StreamBridge, StreamEvent

logger = logging.getLogger(__name__)


@dataclass
class _RunStream:
    events: list[StreamEvent] = field(default_factory=list)  # 表示这个run 的所有事件
    ts: list[int] = field(default_factory=list)
    seq: list[int] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    ended: bool = False
    start_offset: int = 0  # 表示当前events 中第一个事件的索引位置，可能不为 0，因为会删除旧的事件


class MemoryStreamBridge(StreamBridge):
    """Per-run in-memory event log implementation.

    Events are retained for a bounded time window per run so late subscribers
    and reconnecting clients can replay buffered events from ``Last-Event-ID``.
    """

    def __init__(self, *, queue_maxsize: int = 256) -> None:
        self._maxsize = queue_maxsize
        self._streams: dict[str, _RunStream] = {}  # 表示每个run 的事件流
        self._counters: dict[str, int] = {}  # 表示每个run 的事件计数器，用于生成事件id

    # -- helpers ---------------------------------------------------------------

    def _get_or_create_stream(self, run_id: str) -> _RunStream:
        if run_id not in self._streams:
            self._streams[run_id] = _RunStream()
            self._counters[run_id] = 0
        return self._streams[run_id]

    def _next_id(self, run_id: str) -> str:
        self._counters[run_id] = self._counters.get(run_id, 0) + 1  # 表示当前是第几次发布事件
        ts = int(time.time() * 1000)
        seq = self._counters[run_id] - 1  # 表示当前事件的序列号，对应时间列表中的索引位置
        return f"{ts}-{seq}"

    @staticmethod
    def _parse_event_seq(event_id: str) -> int | None:
        """Extract the per-run sequence number from a ``{ts}-{seq}`` event id.

        ``seq`` (assigned by :meth:`_next_id`) increases by one per published
        event, so it equals the event's absolute offset within the run. Returns
        ``None`` for ids that do not match the expected format.
        "1690000000123-40" → ("1690000000123", "-", "40")
        """
        _, sep, seq_text = event_id.rpartition("-")
        if not sep:
            return None
        try:
            return int(seq_text)
        except ValueError:
            return None

    def _resolve_start_offset(self, stream: _RunStream, last_event_id: str | None) -> int:
        """断开连接时，根据last_event_id 解析出最新的start_offset"""
        if last_event_id is None:
            return stream.start_offset

        # Event ids embed a per-run, monotonically increasing ``seq`` that equals
        # the event's absolute offset, so locate the event by arithmetic in O(1)
        # rather than scanning the retained buffer. The id is verified at the
        # computed index, so a stale/evicted/foreign/malformed id still falls back
        # to replay-from-earliest — identical to the previous linear scan.
        seq = self._parse_event_seq(last_event_id)
        if seq is not None:
            local_index = seq - stream.start_offset
            if 0 <= local_index < len(stream.events) and stream.events[local_index].id == last_event_id:
                # 双重确认，确保找到的是正确的事件，找到后返回下一个事件的索引位置
                return stream.start_offset + local_index + 1

        if stream.events:  # 确保events 不为空
            # 如果没有找到，说明事件不存在，返回当前events 中第一个事件的索引位置
            logger.warning(
                "last_event_id=%s not found in retained buffer; replaying from earliest retained event",
                last_event_id,
            )
        return stream.start_offset

    async def stream_exists(self, run_id: str) -> bool:
        """Return whether the in-process event log still has data for *run_id*."""
        return run_id in self._streams

    # -- StreamBridge API ------------------------------------------------------

    async def publish(self, run_id: str, event: str, data: Any) -> None:
        """发布事件：将事件添加到events 中，更新ts 和seq 列表，通知所有订阅者，生产者调用"""
        stream = self._get_or_create_stream(run_id)
        entry = StreamEvent(id=self._next_id(run_id), event=event, data=data)  # 定义新的事件
        async with stream.condition:  # 加锁，确保线程安全
            stream.events.append(entry)
            if len(stream.events) > self._maxsize:  # 如果events 中事件数量超过最大容量，删除旧的事件
                overflow = len(stream.events) - self._maxsize
                del stream.events[:overflow]
                stream.start_offset += overflow
            stream.condition.notify_all()  # 通知所有订阅者，更新事件列表

    async def publish_end(self, run_id: str) -> None:
        """发布结束事件：通知所有订阅者，生产者调用"""
        stream = self._get_or_create_stream(run_id)
        async with stream.condition:
            stream.ended = True
            stream.condition.notify_all()

    async def subscribe(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
        heartbeat_interval: float = 15.0,
    ) -> AsyncIterator[StreamEvent]:
        stream = self._get_or_create_stream(run_id)
        async with stream.condition:
            next_offset = self._resolve_start_offset(stream, last_event_id)  # 解析出下一个事件的索引位置

        while True:
            async with stream.condition:
                if next_offset < stream.start_offset:
                    logger.warning(
                        "subscriber for run %s fell behind retained buffer; resuming from offset %s",
                        run_id,
                        stream.start_offset,
                    )
                    next_offset = stream.start_offset  # 重置下一个事件的索引位置为当前events 中第一个事件的索引位置，当之前的已经被删除时，需要从头开始播放

                local_index = next_offset - stream.start_offset  # 计算在当前事件列表中的索引位置
                if 0 <= local_index < len(stream.events):
                    entry = stream.events[local_index]  # 从events 中获取下一个事件
                    next_offset += 1  # 更新下一个事件的索引位置为当前事件的索引位置加1
                elif stream.ended:
                    entry = END_SENTINEL
                else:
                    try:
                        # 等待下一个事件，超时时间为heartbeat_interval 秒
                        # stream.condition.wait() 是"释放锁 + 暂停协程 + 等通知 + 被叫醒后重新拿锁"
                        # asyncio.wait_for(..., timeout=...) 包一层超时——超过时间没被叫醒就抛 TimeoutError，这是心跳机制。
                        await asyncio.wait_for(stream.condition.wait(), timeout=heartbeat_interval)
                    except TimeoutError:
                        entry = HEARTBEAT_SENTINEL  # 如果超时，返回心跳事件
                    else:
                        continue  # 正常被唤醒，说明有新的事件，重新检查

            if entry is END_SENTINEL:  # 如果是结束事件，说明没有更多事件了，返回结束事件，结束订阅
                yield END_SENTINEL
                return
            yield entry  # 否则，返回当前事件

    async def cleanup(self, run_id: str, *, delay: float = 0) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        self._streams.pop(run_id, None)
        self._counters.pop(run_id, None)

    async def close(self) -> None:
        self._streams.clear()
        self._counters.clear()
