"""A bounded thread pool for synchronous SQLAlchemy calls."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial

executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="db")


async def run_db(function, /, *args, **kwargs):
    """Run a synchronous repository function off the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, partial(function, *args, **kwargs))
