from __future__ import annotations
import asyncio
from collections import defaultdict
from dataclasses import dataclass
from tabulate import tabulate
from typing import ClassVar, Union, List, Optional, Tuple, Iterator
import pydantic
from spectacles.client import LookerClient
from spectacles.lookml import CompiledExplore, Dimension, Explore
from spectacles.exceptions import SpectaclesException, SqlError
from spectacles.logger import GLOBAL_LOGGER as logger
from spectacles.printer import print_header
from spectacles.utils import consume_queue, halt_queue
from spectacles.types import QueryResult

QUERY_TASK_LIMIT = 250
DEFAULT_CHUNK_SIZE = 500
ProfilerTableRow = Tuple[str, str, float, int, str]


@dataclass
class Query:
    explore: Explore
    dimensions: tuple[Dimension, ...]
    query_id: int | None = None
    explore_url: str | None = None
    errored: bool | None = None
    chunk_size: int = DEFAULT_CHUNK_SIZE
    # TODO: Remove this later if we don't need it
    count: ClassVar[dict] = defaultdict(lambda: defaultdict(int))

    def __post_init__(self) -> None:
        Query.count[self.explore.name][len(self.dimensions)] += 1
        # Confirm that all dimensions are from the Explore associated here
        if len(set((d.model_name, d.explore_name) for d in self.dimensions)) > 1:
            raise ValueError("All Dimensions must be from the same model and explore")
        elif self.dimensions[0].explore_name != self.explore.name:
            raise ValueError("Dimension.explore_name must equal Query.explore.name")
        elif self.dimensions[0].model_name != self.explore.model_name:
            raise ValueError("Dimension.model_name must equal Query.explore.model_name")

    def __repr__(self) -> str:
        return f"Query(explore={self.explore.name} n={len(self.dimensions)})"

    async def create(self, client: LookerClient) -> None:
        result = await client.create_query(
            model=self.dimensions[0].model_name,
            explore=self.dimensions[0].explore_name,
            dimensions=[dimension.name for dimension in self.dimensions],
            fields=["id", "share_url"],
        )
        self.query_id = result["id"]
        self.explore_url = result["share_url"]

    def divide(self) -> Iterator[Query]:
        if not self.errored:
            raise TypeError("Query.errored must be True to divide")
        elif len(self.dimensions) == 1:
            raise ValueError("Can't divide, Query has only one dimension")

        midpoint = len(self.dimensions) // 2
        if midpoint > self.chunk_size:
            for i in range(0, len(self.dimensions), self.chunk_size):
                yield Query(self.explore, self.dimensions[i : i + self.chunk_size])
        else:
            yield Query(self.explore, self.dimensions[:midpoint])
            yield Query(self.explore, self.dimensions[midpoint:])


@dataclass(frozen=True)
class ProfilerResult:
    """Stores the data needed to display results for the query profiler."""

    lookml_obj: Union[Dimension, Explore]
    runtime: float
    query: Query

    def format(self) -> ProfilerTableRow:
        """Return data in a format suitable for tabulate to print."""
        return (
            self.lookml_obj.__class__.__name__.lower(),
            self.lookml_obj.name,
            self.runtime,
            self.query.query_id,
            self.query.explore_url,
        )


def print_profile_results(
    results: List[ProfilerResult], runtime_threshold: int
) -> None:
    """Defined here instead of in .printer to avoid circular type imports."""
    HEADER_CHAR = "."
    print_header("Query profiler results", char=HEADER_CHAR, leading_newline=False)
    if results:
        results_by_runtime = sorted(
            results,
            key=lambda x: x.runtime if x.runtime is not None else -1,
            reverse=True,
        )
        output = tabulate(
            [result.format() for result in results_by_runtime],
            headers=[
                "Type",
                "Name",
                "Runtime (s)",
                "Query IDs",
                "Explore From Here",
            ],
            tablefmt="github",
            numalign="left",
            floatfmt=".1f",
        )
    else:
        output = f"All queries completed in less than {runtime_threshold} " "seconds."
    logger.info(output)
    print_header(HEADER_CHAR, char=HEADER_CHAR)


class SqlValidator:
    """Runs and validates the SQL for each selected LookML dimension.

    Args:
        client: Looker API client.
        project: Name of the LookML project to validate.
        concurrency: The number of simultaneous queries to run.
        runtime_threshold: When profiling, only display queries lasting longer
            than this.

    Attributes:
        project: LookML project object representation.
        query_tasks: Mapping of query task IDs to LookML objects

    """

    def __init__(
        self,
        client: LookerClient,
        concurrency: int = 10,
        runtime_threshold: int = 5,
    ):
        self.client = client
        self.concurrency = concurrency
        self.runtime_threshold = runtime_threshold
        self._task_to_query: dict[str, Query] = {}
        self._long_running_queries: List[ProfilerResult] = []

    async def compile_sql(self, explore: Explore) -> CompiledExplore:
        if not explore.dimensions:
            raise AttributeError(
                "Explore object is missing dimensions, "
                "meaning this query won't have fields and will error. "
                "Often this happens because you didn't include dimensions "
                "when you built the project."
            )
        dimensions = [dimension.name for dimension in explore.dimensions]
        # Create a query that includes all dimensions
        query = await self.client.create_query(
            explore.model_name, explore.name, dimensions, fields=["id", "share_url"]
        )
        sql = await self.client.run_query(query["id"])
        return CompiledExplore.from_explore(explore, sql)

    async def search(
        self, explores: tuple[Explore, ...], fail_fast: bool, profile: bool = False
    ):
        queries_to_run: asyncio.Queue[Optional[Query]] = asyncio.Queue()
        running_queries: asyncio.Queue[str] = asyncio.Queue()
        query_slot = asyncio.Semaphore(self.concurrency)

        print("Starting up the workers")
        workers = (
            asyncio.create_task(
                self._run_query(queries_to_run, running_queries, query_slot),
                name="run_query",
            ),
            asyncio.create_task(
                self._get_query_results(
                    queries_to_run, running_queries, fail_fast, query_slot
                ),
                name="get_query_results",
            ),
        )

        try:
            print("Populating the queue")
            for explore in explores:
                queries_to_run.put_nowait(Query(explore, tuple(explore.dimensions)))

            # Wait for all work to complete
            await queries_to_run.join()
            await running_queries.join()
            logger.debug("Successfully joined all queues")
        except KeyboardInterrupt:
            logger.info(
                "\n\n" + "Please wait, asking Looker to cancel any running queries..."
            )
            task_ids = []
            while not running_queries.empty():
                task_id = running_queries.get_nowait()
                task_ids.append(task_id)
                await self.client.cancel_query_task(task_id)
            if task_ids:
                message = (
                    f"Attempted to cancel {len(task_ids)} running "
                    f"{'query' if len(task_ids) == 1 else 'queries'}."
                )
            else:
                message = (
                    "No queries were running at the time so nothing was cancelled."
                )
            raise SpectaclesException(
                name="validation-keyboard-interrupt",
                title="SQL validation was manually interrupted.",
                detail=message,
            )
        finally:
            # Shut down the workers gracefully
            for worker in workers:
                worker.cancel()
            results = await asyncio.gather(*workers, return_exceptions=True)
            for result in results:
                if isinstance(result, asyncio.CancelledError):
                    pass
                elif isinstance(result, Exception):
                    raise result

        if profile:
            print_profile_results(self._long_running_tests, self.runtime_threshold)

    async def _run_query(
        self,
        queries_to_run: asyncio.Queue[Optional[Query]],
        running_queries: asyncio.Queue[str],
        query_slot: asyncio.Semaphore,
    ) -> None:
        try:
            # End execution if a sentinel is received from the queue
            while (query := await queries_to_run.get()) is not None:
                await query_slot.acquire()
                await query.create(self.client)
                print(f"Running query {query!r} [qid={query.query_id}]")
                if query.query_id is None:
                    raise TypeError(
                        "Query.query_id cannot be None, "
                        "run Query.create to get a query ID"
                    )
                task_id = await self.client.create_query_task(query.query_id)
                self._task_to_query[task_id] = query
                running_queries.put_nowait(task_id)

            logger.debug("Received sentinel, shutting down")

        except Exception:
            logger.debug(
                "Encountered an exception while running a query:", exc_info=True
            )
            raise
        finally:
            # This only gets called if a sentinel is received or exception is raised.
            # We need to mark all remaining tasks as finished so Queue.join can unblock
            logger.debug("Marking all tasks in queries_to_run queue as done")
            halt_queue(queries_to_run)

    async def _get_query_results(
        self,
        queries_to_run: asyncio.Queue[Optional[Query]],
        running_queries: asyncio.Queue[str],
        fail_fast: bool,
        query_slot: asyncio.Semaphore,
    ) -> None:
        try:
            while True:
                task_ids = consume_queue(running_queries, limit=QUERY_TASK_LIMIT)
                if not task_ids:
                    logger.debug("No running queries, waiting for one to start...")
                    await asyncio.sleep(0.5)
                    continue

                raw = await self.client.get_query_task_multi_results(task_ids)
                for task_id, result in raw.items():
                    try:
                        query_result = QueryResult.parse_obj(result)
                    except pydantic.ValidationError as validation_error:
                        logger.debug(
                            f"Unable to parse unexpected Looker API response format: {result}"
                        )
                        raise SpectaclesException(
                            name="unexpected-query-result-format",
                            title="Encountered an unexpected query result format.",
                            detail=(
                                "Unable to extract error details from the Looker API's "
                                "response. The unexpected response has been logged."
                            ),
                        ) from validation_error
                    logger.debug(
                        f"Query task {task_id} status is: {query_result.status}"
                    )
                    if query_result.status == "complete":
                        query_slot.release()
                        self._task_to_query[task_id].errored = False
                        queries_to_run.task_done()
                    elif query_result.status == "error":
                        query_slot.release()
                        query = self._task_to_query[task_id]
                        query.errored = True

                        # Fail fast, assign the error(s) to its explore
                        if fail_fast:
                            explore = query.explore
                            explore.queried = True
                            for error in query_result.get_valid_errors():
                                explore.errors.append(
                                    SqlError(
                                        model=explore.model_name,
                                        explore=explore.name,
                                        dimension=None,
                                        sql=query_result.sql,
                                        message=error.full_message,
                                        line_number=error.sql_error_loc.line,
                                        explore_url=query.explore_url,
                                    )
                                )

                        # Make child queries and put them back on the queue
                        elif len(query.dimensions) > 1:
                            n = 0
                            for child in query.divide():
                                n += 1
                                await queries_to_run.put(child)

                        # Assign the error(s) to its dimension
                        else:
                            dimension = query.dimensions[0]
                            dimension.queried = True
                            for error in query_result.get_valid_errors():
                                dimension.errors.append(
                                    SqlError(
                                        model=dimension.model_name,
                                        explore=dimension.explore_name,
                                        dimension=dimension.name,
                                        sql=query_result.sql,
                                        message=error.full_message,
                                        line_number=error.sql_error_loc.line,
                                        lookml_url=dimension.url,
                                        explore_url=query.explore_url,
                                    )
                                )

                        # Indicate there are no more queries or subqueries to run
                        queries_to_run.task_done()
                    else:
                        # Query still running, put the task back on the queue
                        await running_queries.put(task_id)

                # Notify queue that all task IDs were processed
                for _ in range(len(task_ids)):
                    running_queries.task_done()

                await asyncio.sleep(0.5)
        except Exception:
            logger.debug(
                "Encountered an exception while retrieving results:", exc_info=True
            )
            # Put a sentinel on the run query queue to shut it down
            queries_to_run.put_nowait(None)
            # Wait until the sentinel has been consumed and handled
            while not queries_to_run.empty():
                logger.debug("Waiting for the queries_to_run queue to clear")
                await asyncio.sleep(1)
            raise
        finally:
            # This only gets called if an exception is raised.
            # We need to mark all remaining tasks as finished so Queue.join can unblock
            logger.debug("Marking all tasks in running_queries queue as done")
            halt_queue(running_queries)
