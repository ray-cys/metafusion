import asyncio
import weakref
from contextlib import asynccontextmanager


_loop_semaphores = weakref.WeakKeyDictionary()


def _limit_for(config, kind):
    maximum = max(1, int(config.get("runtime", {}).get("max_concurrency", 8)))
    if kind == "plex":
        return min(4, maximum)
    return maximum


def runtime_semaphore(config, kind):
    """Return one per-loop semaphore shared by every nested media task."""
    loop = asyncio.get_running_loop()
    limit = _limit_for(config, kind)
    semaphores = _loop_semaphores.setdefault(loop, {})
    key = (str(kind), limit)
    semaphore = semaphores.get(key)
    if semaphore is None:
        semaphore = asyncio.Semaphore(limit)
        semaphores[key] = semaphore
    return semaphore


@asynccontextmanager
async def runtime_slot(config, kind):
    semaphore = runtime_semaphore(config, kind)
    async with semaphore:
        yield


async def bounded_map(operation, items, limit, *, config=None, kind="nested"):
    """Map an async operation without creating one task for every input."""
    values = list(items)
    if not values:
        return []
    results = [None] * len(values)
    iterator = iter(enumerate(values))

    async def worker():
        for index, value in iterator:
            if config is None:
                results[index] = await operation(value)
            else:
                async with runtime_slot(config, kind):
                    results[index] = await operation(value)

    workers = [
        asyncio.create_task(worker())
        for _ in range(min(max(1, int(limit)), len(values)))
    ]
    try:
        await asyncio.gather(*workers)
    except BaseException:
        for worker_task in workers:
            worker_task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        raise
    return results


async def bounded_callables(operations, limit, *, config=None, kind="nested"):
    async def invoke(operation):
        return await operation()

    return await bounded_map(
        invoke,
        operations,
        limit,
        config=config,
        kind=kind,
    )
