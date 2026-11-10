"""Force database contention during independent manual account connections."""

import asyncio

from messaging.contracts import MessagingSettings
from messaging.service import MessagingService


class SameIdentityAdapter:
    def __init__(self):
        self.started = 0
        self.returned = 0
        self.both_started = asyncio.Event()
        self.release_identity = asyncio.Event()
        self.both_returned = asyncio.Event()

    async def connect(self, account):
        self.started += 1
        if self.started == 2:
            self.both_started.set()
        await self.release_identity.wait()
        self.returned += 1
        if self.returned == 2:
            self.both_returned.set()
        return {"user_id": "111"}

    async def close(self):
        pass


async def test_concurrent_accounts_cannot_bind_the_same_verified_user(tmp_path):
    adapter = SameIdentityAdapter()
    service = MessagingService(MessagingSettings(root=tmp_path), adapter)
    await service.start()
    try:
        operations = [
            await service.connect_account(account_id)
            for account_id in ("own-a", "own-b")
        ]
        async with asyncio.timeout(2):
            await adapter.both_started.wait()
            # An unrelated storage operation makes both uniqueness checks wait.
            # Without a connection-wide lock, both then read the unbound rows
            # before either pending write can acquire the store lock again.
            async with service.store.lock:
                adapter.release_identity.set()
                await adapter.both_returned.wait()
            await asyncio.gather(*(task for _, task in service._connect_tasks.values()))
        accounts = await service.store.accounts()
        assert sum(account.get("user_id") == "111" for account in accounts) == 1
        results = [
            await service.get_operation(item["account_id"], item["operation_id"])
            for item in operations
        ]
        assert sorted(item["state"] for item in results) == [
            "connected",
            "reauth_required",
        ]
        rejected = next(item for item in results if item["state"] == "reauth_required")
        assert rejected["error"]["code"] == "account_already_bound"
    finally:
        await service.close()


async def test_same_account_parallel_connects_share_one_operation_and_binding(tmp_path):
    class SwitchingIdentityAdapter:
        def __init__(self):
            self.calls = 0
            self.release_identity = asyncio.Event()

        async def connect(self, account):
            self.calls += 1
            await self.release_identity.wait()
            return {"user_id": "111" if self.calls == 1 else "222"}

        async def close(self):
            pass

    adapter = SwitchingIdentityAdapter()
    service = MessagingService(MessagingSettings(root=tmp_path), adapter)
    await service.start()
    try:
        await service.store.save_account(
            {"account_id": "own", "user_id": None, "state": "disconnected"}
        )
        async with service.store.lock:
            requests = [
                asyncio.create_task(service.connect_account("own")) for _ in range(2)
            ]
            # Both calls reach the same initial account lookup while storage is busy.
            for _ in range(3):
                await asyncio.sleep(0)
        operations = await asyncio.gather(*requests)
        adapter.release_identity.set()
        await asyncio.gather(*list(service._tasks))
        assert (await service.store.account("own"))["user_id"] == "111"
        assert adapter.calls == 1
        assert operations[0]["operation_id"] == operations[1]["operation_id"]
    finally:
        await service.close()
