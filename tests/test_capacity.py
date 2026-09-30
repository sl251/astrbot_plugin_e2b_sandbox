import asyncio
import unittest

from runtime_capacity import RuntimeCapacity, SandboxBusyError


class CapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_running_slot_is_reused_without_double_counting(self):
        capacity = RuntimeCapacity(limit=1)
        await capacity.acquire("a")
        await capacity.acquire("a")
        self.assertEqual(capacity.occupied, {"a"})

    async def test_fifo_and_bounded_queue(self):
        capacity = RuntimeCapacity(limit=1, max_queue=2)
        await capacity.acquire("a")
        order = []
        async def worker(session):
            await capacity.acquire(session)
            order.append(session)
            await capacity.release(session)
        first = asyncio.create_task(worker("b"))
        await asyncio.sleep(0)
        second = asyncio.create_task(worker("c"))
        await asyncio.sleep(0)
        with self.assertRaises(SandboxBusyError):
            await capacity.acquire("d")
        await capacity.release("a")
        await asyncio.gather(first, second)
        self.assertEqual(order, ["b", "c"])
        self.assertFalse(capacity.occupied)
        self.assertEqual(capacity.waiting, 0)

    async def test_cancelled_waiter_does_not_block_next_request(self):
        capacity = RuntimeCapacity(limit=1)
        await capacity.acquire("a")
        waiter = asyncio.create_task(capacity.acquire("b"))
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        self.assertEqual(capacity.waiting, 0)
        await capacity.release("a")
        await capacity.acquire("c")
        self.assertEqual(capacity.occupied, {"c"})

    async def test_timeout_does_not_consume_capacity(self):
        capacity = RuntimeCapacity(limit=1, wait_timeout=0.01, occupied=["a"])
        with self.assertRaises(SandboxBusyError):
            await capacity.acquire("b")
        self.assertEqual(capacity.occupied, {"a"})
        self.assertEqual(capacity.waiting, 0)

    async def test_zero_queue_returns_busy(self):
        capacity = RuntimeCapacity(limit=1, max_queue=0, occupied=["a"])
        with self.assertRaises(SandboxBusyError):
            await capacity.acquire("b")

    async def test_lowered_limit_keeps_existing_reservations(self):
        capacity = RuntimeCapacity(limit=3, max_queue=0, occupied=["a", "b"])
        await capacity.configure(1, 0, 1)
        with self.assertRaises(SandboxBusyError):
            await capacity.acquire("c")
        await capacity.release("a")
        with self.assertRaises(SandboxBusyError):
            await capacity.acquire("c")
        await capacity.release("b")
        await capacity.acquire("c")
