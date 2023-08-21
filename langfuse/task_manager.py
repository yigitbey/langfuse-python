import atexit
from datetime import datetime, timedelta
from enum import Enum
import logging
import queue
import threading


logger = logging.getLogger("Langfuse")
logger.setLevel(logging.INFO)


class Task:
    def __init__(self, task_id, function, predecessor_id: str = None):
        self.task_id = task_id
        self.predecessor_id = predecessor_id
        self.function = function
        self.result = None
        self.timestamp = None
        self.lock = threading.Lock()
        self.status = TaskStatus.UNSCHEDULED


class TaskStatus(Enum):
    SUCCESS = "success"
    FAIL = "fail"
    UNSCHEDULED = "unscheduled"


class TaskManager:
    def __init__(self, max_task_queue_size=10_000, max_task_age=600):
        self.max_task_queue_size = max_task_queue_size
        self.queue = queue.Queue(max_task_queue_size)
        self.consumer_thread = None
        self.result_mapping = {}
        self.max_task_age = max_task_age
        self.init_resources()

        # cleans up when the python interpreter closes
        atexit.register(self.join)

    def init_resources(self):
        self.consumer_thread = Consumer(self.queue, self.result_mapping, self.max_task_age)
        self.consumer_thread.start()

    def add_task(self, task_id, function, predecessor_id=None):
        try:
            logger.info(f"Adding task {task_id} with predecessor {predecessor_id}")
            if self.consumer_thread is None or not self.consumer_thread.is_alive():
                self.init_resources()
            task = Task(task_id, function, predecessor_id)

            self.queue.put(task, block=False)
            logger.info(f"Task {task_id} added to queue")
        except queue.Full:
            logger.warning("analytics-python queue is full")
            return False
        except Exception as e:
            logger.error(f"Exception in adding task {task_id} {e}")
            return False

    def flush(self):
        """Forces a flush from the internal queue to the server"""
        logger.info("flushing queue")
        queue = self.queue
        size = queue.qsize()
        queue.join()
        # Note that this message may not be precise, because of threading.
        logger.info("successfully flushed about %s items.", size)

    def join(self):
        """Ends the consumer thread once the queue is empty.
        Blocks execution until finished
        """
        logger.info("joining consumer thread")
        self.consumer_thread.pause()
        try:
            self.consumer_thread.join()
        except RuntimeError:
            # consumer thread has not started
            pass
        logger.info("consumer thread joined")

    def shutdown(self):
        """Flush all messages and cleanly shutdown the client"""
        logger.info("shutdown initiated")
        self.flush()
        self.join()
        logger.info("shutdown completed")

    def get_result(self, task_id):
        try:
            return self.result_mapping.get(task_id)
        except Exception as e:
            logger.error(f"Exception in getting result for task {task_id} {e}")


class Consumer(threading.Thread):
    def __init__(self, queue, result_mapping, max_task_age):
        """Create a consumer thread."""

        threading.Thread.__init__(self)
        # Make consumer a daemon thread so that it doesn't block program exit
        self.daemon = True
        self.queue = queue
        # It's important to set running in the constructor: if we are asked to
        # pause immediately after construction, we might set running to True in
        # run() *after* we set it to False in pause... and keep running
        # forever.
        self.running = True
        self.result_mapping = result_mapping
        self.max_task_age = max_task_age

    def run(self):
        """Runs the consumer."""
        logger.info("consumer is running...")
        while self.running:
            try:
                logger.info("consumer looping")
                self._prune_old_tasks(self.max_task_age)

                # elapsed = time.monotonic.monotonic() - start_time
                task = self.queue.get(block=True, timeout=1)

                self.result_mapping[task.task_id] = task

                logger.info(f"Task {task.task_id} received from the queue")

                self._execute_task(task)
                logger.info(f"Task {task.task_id} done")
                self.queue.task_done()

            except queue.Empty:
                break

        logger.debug("consumer exited.")

    def pause(self):
        """Pause the consumer."""
        self.running = False

    def _execute_task(self, task: Task):
        try:
            logger.info(f"Task {task.task_id} executing")

            result = None
            with task.lock:
                try:
                    result = task.function()
                    self.result_mapping[task.task_id].result = result
                    self.result_mapping[task.task_id].status = TaskStatus.SUCCESS
                    self.result_mapping[task.task_id].timestamp = datetime.now()
                    logger.info(f"Task {task.task_id} done with result {result}")
                except Exception as e:
                    self.result_mapping[task.task_id].result = e
                    self.result_mapping[task.task_id].status = TaskStatus.FAIL
                    self.result_mapping[task.task_id].timestamp = datetime.now()
                    logger.info(f"Task {task.task_id} failed with exception {e} ")
        except Exception as e:
            logger.error(f"Exception in the task {task.task_id} {e}")

    def _prune_old_tasks(self, delta: int):
        try:
            logger.info("Pruning old tasks")
            now = datetime.now()

            to_remove = [task_id for task_id, task in self.result_mapping.items() if task.status in [TaskStatus.SUCCESS, TaskStatus.FAIL] and task.timestamp and now - task.timestamp > timedelta(seconds=delta)]
            for task_id in to_remove:
                self.result_mapping.pop(task_id, None)
                logger.info(f"Task {task_id} pruned due to age")
        except Exception as e:
            logger.error(f"Exception in pruning old tasks {e}")
