from typing import Any, Dict, List, Optional, Tuple
import dataclasses
import enum
import getpass
import os
import queue
import re
import socket
import threading
import time

from reallm.api.config import config_system as config_pkg
from reallm.base.gpu_utils import set_cuda_device
import reallm.base.cluster
import reallm.base.logging as logging
import reallm.base.monitor
import reallm.base.name_resolve
import reallm.base.names
import reallm.base.network
import reallm.base.timeutil

logger = logging.getLogger("worker")

_MAX_SOCKET_CONCURRENCY = 1000
WORKER_WAIT_FOR_CONTROLLER_SECONDS = 3600
WORKER_JOB_STATUS_LINGER_SECONDS = 60
TRACER_SAVE_INTERVAL_SECONDS = 60


class WorkerException(Exception):

    def __init__(self, worker_name, worker_status, scenario):
        super(WorkerException, self).__init__(f"Worker {worker_name} is {worker_status} while {scenario}")
        self.worker_name = worker_name
        self.worker_status = worker_status
        self.scenario = scenario


class WorkerServerStatus(str, enum.Enum):
    """List of all possible Server status. This is typically set by workers hosting the server, and
    read by the controller.
    """

    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"

    UNKNOWN = "UNKNOWN"  # CANNOT be set.
    INTERRUPTED = "INTERRUPTED"
    ERROR = "ERROR"
    LOST = "LOST"  # CANNOT be set


class NoRequstForWorker(Exception):
    pass


class WorkerServerTaskQueue:

    def try_get_request(self) -> Tuple[str, Dict[str, Any]]:
        raise NotImplementedError()

    def respond(self, response):
        raise NotImplementedError()

    @property
    def port(self) -> int:
        return -1


class WorkerServer:
    """A light-weight implementation of an RPC server.

    Note that this server only allows a single client connection for now, as that is sufficient for
    workers which respond to the controller only.

    Example:
        # Server side.
        server = RpcServer(port)
        server.register_handler('foo', foo)
        while True:
            server.handle_requests()

        # Client side.
        client = RpcClient(host, port)
        client.request('foo', x=42, y='str') # foo(x=42, y='str') will be called on the server side.
    """

    def __init__(self, worker_name, experiment_name, trial_name, task_queue: WorkerServerTaskQueue):
        """Specifies the name of the worker that WorkerControlPanel can used to find and manage.
        Args:
            worker_name: Typically "<worker_type>/<worker_index>".
        """
        self.__worker_name = worker_name
        self.__experiment_name = experiment_name
        self.__trial_name = trial_name

        self.__task_queue = task_queue

        self.__handlers = {}
        host_ip = socket.gethostbyname(socket.gethostname())

        try:
            controller_status = reallm.base.name_resolve.wait(
                reallm.base.names.worker_status(experiment_name, trial_name, "ctl"),
                timeout=WORKER_WAIT_FOR_CONTROLLER_SECONDS,
            )
        except TimeoutError:
            raise TimeoutError(
                f"Worker ({experiment_name, trial_name, worker_name}) connect to controller timeout from host {socket.gethostname()}."
            )

        if controller_status != "READY":
            raise RuntimeError(f"Abnormal controller state on experiment launch {controller_status}.")

        if experiment_name is not None and trial_name is not None:
            key = reallm.base.names.worker(experiment_name, trial_name, worker_name)
            address = f"{host_ip}:{self.__task_queue.port}"
            reallm.base.name_resolve.add(key, address, keepalive_ttl=10, delete_on_exit=True)
            logger.info("Added name_resolve entry %s for worker server at %s", key, address)

    def register_handler(self, command, fn):
        """Registers an RPC command. The handler `fn` shall be called when `self.handle_requests()` sees an
        incoming command of the registered type.
        """
        if command in self.__handlers:
            raise KeyError(f"Command '{command}' exists")
        self.__handlers[command] = fn

    def handle_requests(self, max_count=None):
        """Handles queued requests in order, optionally limited by `max_count`.

        Returns:
            The count of requests handled.
        """
        count = 0
        while max_count is None or count < max_count:
            try:
                command, kwargs = self.__task_queue.try_get_request()
            except NoRequstForWorker:
                # Currently no request in the queue.
                break
            logger.debug("Handle request %s with kwargs %s", command, kwargs)
            if command in self.__handlers:
                try:
                    response = self.__handlers[command](**kwargs)
                    logger.debug("Handle request: %s, ok", command)
                except WorkerException:
                    raise
                except Exception as e:
                    logger.error("Handle request: %s, error", command)
                    logger.error(e, exc_info=True)
                    response = e
            else:
                logger.error("Handle request: %s, no such command", command)
                response = KeyError(f"No such command: {command}")
            self.__task_queue.respond(response)
            logger.debug("Handle request: %s, sent reply", command)
            count += 1
        return count

    def set_status(self, status: WorkerServerStatus):
        """On graceful exit, worker status is cleared."""
        reallm.base.name_resolve.add(
            reallm.base.names.worker_status(
                experiment_name=self.__experiment_name,
                trial_name=self.__trial_name,
                worker_name=self.__worker_name,
            ),
            value=status.value,
            keepalive_ttl=WORKER_JOB_STATUS_LINGER_SECONDS,  # Job Status lives one minutes after worker exit.
            replace=True,
            delete_on_exit=False,
        )


class WorkerControlPanelRequester:

    class Future:

        def result(self, timeout=None):
            raise NotImplementedError()

    def async_request(self,
                      worker_name: str,
                      address: str,
                      command: str,
                      wait_for_response: bool = True,
                      **kwargs) -> Future:
        raise NotImplementedError()


class WorkerControlPanel:
    """A class that defines the management utilities to all the workers of an experiment trial."""

    @dataclasses.dataclass
    class Response:
        worker_name: str
        result: WorkerControlPanelRequester.Future
        timed_out: bool = False

    def __init__(self, experiment_name, trial_name, requester: WorkerControlPanelRequester):
        self.__closed = False

        self.__experiment_name = experiment_name
        self.__trial_name = trial_name
        self.__worker_addresses = {}

        self.__requester = requester

        self.__logger = logging.getLogger("worker control panel")

    def __del__(self):
        if not self.__closed:
            self.close()
            self.__closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self.__closed:
            self.close()
            self.__closed = True

    def close(self):
        self.__logger.info("Closing worker control panel.")

    @staticmethod
    def name(worker_type, worker_index):
        return f"{worker_type}/{worker_index}"

    @staticmethod
    def parse_name(worker_name):
        type_, index = worker_name.split("/")
        return type_, index

    @property
    def worker_names(self) -> List[str]:
        """Returns current connected workers. A WorkerControlPanel initializes with no connected workers.
        Workers are connected via either self.connect(names), or self.auto_connect().
        """
        return list(self.__worker_addresses.keys())

    def connect(
        self,
        worker_names: List[str],
        timeout=None,
        raises_timeout_error=False,
        reconnect=False,
        progress=False,
    ) -> List[str]:
        """Waits until all the workers specified by the given names are ready for receiving commands.

        Args:
            worker_names: A list of workers to connect.
            timeout: The maximum waiting time in seconds, or None if infinite.
            raises_timeout_error: If True, any connection failure will result in the raise of an TimeoutError.
                If False, such exception can be detected via the returned succeeded list.
            reconnect: If True, this will reconnect to the workers that has already connected. If False, any
                worker in `worker_names` that has already connected will be ignored.
            progress: Whether to show a progress bar.

        Returns:
            A list of successfully connected or reconnected workers. If a specified worker is missing, it can
            either be that it is already connected and `reconnect` is False, or that the connection timed out.
        """
        rs = []
        deadline = time.monotonic() + (timeout or 0)
        if progress:
            try:
                import tqdm

                worker_names = tqdm.tqdm(worker_names, leave=False)
            except ModuleNotFoundError:
                pass
        for name in worker_names:
            if name in self.__worker_addresses:
                if reconnect:
                    del self.__worker_addresses[name]
                else:
                    continue
            try:
                if timeout is not None:
                    timeout = max(0, deadline - time.monotonic())
                self.__logger.info(f"Connecting to worker {name}, timeout {timeout}")
                server_address = reallm.base.name_resolve.wait(base.names.worker(
                    self.__experiment_name, self.__trial_name, name),
                                                               timeout=timeout)
                self.__logger.info(f"Connecting to worker {name} done")
            except TimeoutError as e:
                if raises_timeout_error:
                    raise e
                continue
            # self.__worker_addresses[name] stores address
            self.__worker_addresses[name] = server_address
            rs.append(name)
        return rs

    def auto_connect(self) -> List[str]:
        """Auto-detects available workers belonging to the experiment trial, and connects to them.

        Returns:
            Names of successfully connected workers.
        """
        name_root = reallm.base.names.worker_root(self.__experiment_name, self.__trial_name)
        worker_names = [r[len(name_root):] for r in reallm.base.name_resolve.find_subtree(name_root)]
        return self.connect(worker_names, timeout=0, raises_timeout_error=True)

    def request(self, worker_name: str, command, **kwargs) -> Any:
        """Sends an request to the specified worker."""
        address = self.__worker_addresses[worker_name]
        return self.__requester.async_request(worker_name, address, command, **kwargs).result()

    def group_request(
        self,
        command,
        worker_names: Optional[List[str]] = None,
        worker_regex: Optional[str] = None,
        timeout=None,
        progress=False,
        worker_kwargs: Optional[List[Dict[str, Any]]] = None,
        wait_response=True,
        **kwargs,
    ) -> List[Response]:
        """Requests selected workers, or all connected workers if not specified.

        Args:
            command: RPC command.
            worker_names: Optional selection of workers.
            worker_regex: Optional regex selector of workers.
            timeout: Optional timeout.
            progress: Whether to show a progress bar.
            worker_kwargs: RPC arguments, but one for each worker instead of `kwargs` where every worker share
                the arguments. If this is specified, worker_names must be specified, and worker_regex, kwargs
                must be None.
            wait_response: Whether to wait for server response.
            kwargs: RPC arguments.
        """
        selected = self.worker_names
        if worker_names is not None:
            assert len(set(worker_names).difference(selected)) == 0
            selected = worker_names
        if worker_regex is not None:
            selected = [x for x in selected if re.fullmatch(worker_regex, x)]
        if worker_kwargs is not None:
            assert worker_names is not None
            assert worker_regex is None
            assert len(kwargs) == 0
            assert len(worker_names) == len(worker_kwargs), f"{len(worker_names)} != {len(worker_kwargs)}"
        else:
            worker_kwargs = [kwargs for _ in selected]

        # connect _MAX_SOCKET_CONCURRENCY sockets at most
        rs = []
        deadline = time.monotonic() + (timeout or 0)
        for j in range(0, len(selected), _MAX_SOCKET_CONCURRENCY):
            sub_rs: List[WorkerControlPanel.Response] = []
            sub_selected = selected[j:j + _MAX_SOCKET_CONCURRENCY]
            sub_worker_kwargs = worker_kwargs[j:j + _MAX_SOCKET_CONCURRENCY]
            for name, kwargs in zip(sub_selected, sub_worker_kwargs):
                address = self.__worker_addresses[name]
                result_fut = self.__requester.async_request(name, address, command, wait_response, **kwargs)
                sub_rs.append(WorkerControlPanel.Response(worker_name=name, result=result_fut))

            if not wait_response:
                continue

            bar = range(len(sub_rs))
            if progress:
                try:
                    import tqdm

                    bar = tqdm.tqdm(bar, leave=False)
                except ModuleNotFoundError:
                    pass
            for r, _ in zip(sub_rs, bar):
                if timeout is not None:
                    timeout = max(0, deadline - time.monotonic())
                try:
                    r.result = r.result.result(timeout=timeout)
                except TimeoutError:
                    r.timed_out = True
            rs.extend(sub_rs)
        return rs

    def get_worker_status(self, worker_name) -> WorkerServerStatus:
        """Get status of a connected worker.
        Raises:
            ValueError if worker is not connected.
        """
        try:
            status_str = reallm.base.name_resolve.wait(
                reallm.base.names.worker_status(
                    experiment_name=self.__experiment_name,
                    trial_name=self.__trial_name,
                    worker_name=worker_name,
                ),
                timeout=60,
            )
            status = WorkerServerStatus(status_str)
        except reallm.base.name_resolve.NameEntryNotFoundError:
            status = WorkerServerStatus.LOST
        return status

    def pulse(self):
        return {name: self.get_worker_status(name) for name in self.worker_names}


@dataclasses.dataclass
class PollResult:
    # Number of total samples and batches processed by the worker. Specifically:
    # - For an actor worker, sample_count = batch_count = number of env.step()-s being executed.
    # - For a policy worker, number of inference requests being handled, versus how many batches were made.
    # - For a trainer worker, number of samples & batches fed into the trainer (typically GPU).
    sample_count: int
    batch_count: int


class Worker:
    """The worker base class that provides general methods and entry point.

    For simplicity, we use a single-threaded pattern in implementing the worker RPC server. Logic
    of every worker are executed via periodical calls to the poll() method, instead of inside
    another thread or process (e.g. the gRPC implementation). A subclass only needs to implement
    poll() without duplicating the main loop.

    The typical code on the worker side is:
        worker = make_worker()  # Returns instance of Worker.
        worker.run()
    and the later is standardized here as:
        while exit command is not received:
            if worker is started:
                worker.poll()
    """

    def __init__(self, server: Optional[WorkerServer] = None):
        """Initializes a worker server.

        Args:
            server: The RPC server API for the worker to register handlers and poll requests.
        """
        self.__running = False
        self.__exiting = False
        self.config = None
        self.__is_configured = False

        self.__tracer_launched = False

        self._server = server
        if server is not None:
            server.register_handler("configure", self.configure)
            server.register_handler("reconfigure", self.reconfigure)
            server.register_handler("start", self.start)
            server.register_handler("pause", self.pause)
            server.register_handler("exit", self.exit)
            server.register_handler("interrupt", self.interrupt)
            server.register_handler("ping", lambda: "pong")

        self.logger = logging.getLogger("worker")
        self.__worker_type = None
        self.__worker_index = None
        self.__last_successful_poll_time = None
        self.__worker_info = None

        self._start_time_ns = None

        self.__set_status(WorkerServerStatus.READY)

    def __set_status(self, status: WorkerServerStatus):
        if self._server is not None:
            self.logger.info(f"Setting worker server status to {status}")
            self._server.set_status(status)

    @property
    def is_configured(self):
        return self.__is_configured

    def _reconfigure(self, **kwargs) -> config_pkg.WorkerInformation:
        """Implemented by sub-classes."""
        raise NotImplementedError()

    def _configure(self, config) -> config_pkg.WorkerInformation:
        """Implemented by sub-classes."""
        raise NotImplementedError()

    def _poll(self) -> PollResult:
        """Implemented by sub-classes."""
        raise NotImplementedError()

    def _stats(self) -> Dict[str, Any]:
        """Implemented by sub-classes. For wandb logging only"""
        return {}

    def configure(self, config):
        assert not self.__running
        self.logger.info("Configuring with: %s", config)

        r = self._configure(config)
        self.__worker_info = r
        self.__worker_type = r.worker_type
        self.__worker_index = r.worker_index
        self.logger = logging.getLogger(r.worker_type + "-worker", 'colored')
        if r.host_key is not None:
            self.__host_key(
                reallm.base.names.worker_key(experiment_name=r.experiment_name,
                                             trial_name=r.trial_name,
                                             key=r.host_key))
        if r.watch_keys is not None:
            keys = [r.watch_keys] if isinstance(r.watch_keys, str) else r.watch_keys
            self.__watch_keys([
                reallm.base.names.worker_key(experiment_name=r.experiment_name,
                                             trial_name=r.trial_name,
                                             key=k) for k in keys
            ])

        self._tracer_output_file = os.path.join(
            reallm.base.cluster.spec.fileroot,
            "logs",
            getpass.getuser(),
            r.experiment_name,
            r.trial_name,
            "trace_results",
            f"{r.worker_type}-{r.worker_index}.json",
        )
        os.makedirs(os.path.dirname(self._tracer_output_file), exist_ok=True)
        self.__tracer = reallm.base.monitor.get_tracer(
            tracer_entries=int(1e7),
            # max_stack_depth=25,
            ignore_c_function=False,
            ignore_frozen=True,
            log_async=True,
            min_duration=25,
            output_file=self._tracer_output_file,
        )
        self.__tracer_save_freqctrl = reallm.base.timeutil.FrequencyControl(
            frequency_seconds=TRACER_SAVE_INTERVAL_SECONDS)

        self.__is_configured = True
        self.logger.info("Configured successfully")

    def reconfigure(self, **kwargs):
        assert not self.__running
        self.__is_configured = False
        self.logger.info(f"Reconfiguring with: {kwargs}")
        self._reconfigure(**kwargs)
        self.__is_configured = True
        self.logger.info("Reconfigured successfully")

    def start(self):
        self.logger.info("Starting worker")
        self.__running = True
        self.__set_status(WorkerServerStatus.RUNNING)

    def pause(self):
        self.logger.info("Pausing worker")
        self.__running = False
        self.__set_status(WorkerServerStatus.PAUSED)

    def exit(self):
        self.logger.info("Exiting worker")
        import torch.distributed as dist
        if dist.is_initialized():
            dist.destroy_process_group()
        self.__set_status(WorkerServerStatus.COMPLETED)
        self.__exiting = True

    def interrupt(self):
        self.logger.info("Worker interrupted by remote control.")
        self.__set_status(WorkerServerStatus.INTERRUPTED)
        raise WorkerException(worker_name="worker",
                              worker_status=WorkerServerStatus.INTERRUPTED,
                              scenario="running")

    @property
    def tracer(self):
        return self.__tracer

    def run(self):
        self._start_time_ns = time.monotonic_ns()
        self.__last_update_ns = None
        self.logger.info("Running worker now")
        try:
            while not self.__exiting:
                self._server.handle_requests()
                if not self.__running:
                    time.sleep(0.05)
                    continue
                if not self.__is_configured:
                    raise RuntimeError("Worker is not configured")
                if not self.__tracer_launched:
                    # self.logger.info("Launching tracer ... ")
                    self.__tracer.start()
                    self.__tracer_launched = True
                    self.__tracer.save()
                start_time = time.monotonic_ns()
                r = self._poll()
                poll_time = (time.monotonic_ns() - start_time) / 1e9
                wait_seconds = 0.0
                if self.__last_successful_poll_time is not None:
                    # Account the waiting time since the last successful step.
                    wait_seconds = (start_time - self.__last_successful_poll_time) / 1e9
                self.__last_successful_poll_time = time.monotonic_ns()

                if r.sample_count == r.batch_count == 0:
                    # time.sleep(0.002)
                    pass
                else:
                    now = time.monotonic_ns()
                    if self.__last_update_ns is not None:  # Update new stats with 10 seconds frequency.
                        if (now - self.__last_update_ns) / 1e9 >= 10:
                            duration = (time.monotonic_ns() - self._start_time_ns) / 1e9
                            self.__last_update_ns = now
                    else:
                        self.__last_update_ns = now
                if self.__tracer_save_freqctrl.check():
                    # self.logger.info("Tracer Save ... ")
                    self.__tracer.save()
        except KeyboardInterrupt:
            self.exit()
        except Exception as e:
            if isinstance(e, WorkerException):
                raise e
            self.__set_status(WorkerServerStatus.ERROR)
            raise e

    def __host_key(self, key: str):
        self.logger.info(f"Hosting key: {key}")
        reallm.base.name_resolve.add(key, "up", keepalive_ttl=15, replace=True, delete_on_exit=True)

    def __watch_keys(self, keys: List[str]):
        self.logger.info(f"Watching keys: {keys}")
        reallm.base.name_resolve.watch_names(keys, call_back=self.exit)


class MappingThread:
    """Wrapped of a mapping thread.
    A mapping thread gets from up_stream_queue, process data, and puts to down_stream_queue.
    """

    def __init__(self,
                 map_fn,
                 interrupt_flag,
                 upstream_queue,
                 downstream_queue: queue.Queue = None,
                 cuda_device=None):
        """Init method of MappingThread for Policy Workers.

        Args:
            map_fn: mapping function.
            interrupt_flag: main thread sets this value to True to interrupt the thread.
            upstream_queue: the queue to get data from.
            downstream_queue: the queue to put data after processing. If None, data will be discarded after processing.
        """
        self.__map_fn = map_fn
        self.__interrupt = interrupt_flag
        self.__upstream_queue = upstream_queue
        self.__downstream_queue = downstream_queue
        self.__thread = threading.Thread(target=self._run, daemon=True)
        self.__cuda_device = cuda_device

    def is_alive(self) -> bool:
        """Check whether the thread is alive.

        Returns:
            alive: True if the wrapped thread is alive, False otherwise.
        """
        return self.__interrupt or self.__thread.is_alive()

    def start(self):
        """Start the wrapped thread."""
        self.__thread.start()

    def join(self):
        """Join the wrapped thread."""
        self.__thread.join()

    def _run(self):
        if self.__cuda_device is not None:
            set_cuda_device(self.__cuda_device)
        while not self.__interrupt:
            self._run_step()

    def _run_step(self):
        try:
            data = self.__upstream_queue.get(timeout=1)
            data = self.__map_fn(data)
            if self.__downstream_queue is not None:
                self.__downstream_queue.put(data)
        except queue.Empty:
            pass

    def stop(self):
        """Stop the wrapped thread."""
        self.__interrupt = True
        if self.__thread.is_alive():
            self.__thread.join()
