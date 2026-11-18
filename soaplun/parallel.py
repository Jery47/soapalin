"""Small ordered parallel-map helper shared by the copy pipelines."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable, List, Optional, TypeVar, cast

T = TypeVar("T")
R = TypeVar("R")


def map_ordered(
    items: Iterable[T],
    worker: Callable[[int, T], R],
    parallelism: int = 1,
    on_complete: Optional[Callable[[int, R], None]] = None,
) -> List[R]:
    """Run ``worker(index, item)`` and return results in input order.

    ``index`` is 1-based.  ``on_complete(index, result)`` is invoked as soon
    as each item finishes, which lets callers update progress counters.
    """
    values = list(items)
    total = len(values)
    results: List[Optional[R]] = [None] * total
    workers = max(1, int(parallelism or 1))

    if workers <= 1 or total <= 1:
        for index, item in enumerate(values, start=1):
            result = worker(index, item)
            results[index - 1] = result
            if on_complete is not None:
                on_complete(index, result)
        return [cast(R, result) for result in results if result is not None]

    with ThreadPoolExecutor(max_workers=min(workers, total)) as executor:
        futures = {
            executor.submit(worker, index, item): index
            for index, item in enumerate(values, start=1)
        }
        for future in as_completed(futures):
            index = futures[future]
            result = future.result()
            results[index - 1] = result
            if on_complete is not None:
                on_complete(index, result)
    return [cast(R, result) for result in results if result is not None]
