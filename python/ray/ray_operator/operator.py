import logging
import multiprocessing as mp
import queue
import os
import threading
from typing import Any
from typing import Callable
from typing import Dict
from typing import Tuple
from typing import Optional
from typing import Union

import kopf
import yaml

import ray.autoscaler._private.monitor as monitor
from ray._private import services
from ray.autoscaler._private import commands
from ray.ray_operator import operator_utils
from ray.ray_operator.operator_utils import AUTOSCALER_RETRIES_FIELD
from ray.ray_operator.operator_utils import STATUS_AUTOSCALING_EXCEPTION
from ray.ray_operator.operator_utils import STATUS_RUNNING
from ray.ray_operator.operator_utils import STATUS_UPDATING
from ray import ray_constants

logger = logging.getLogger(__name__)

# Queue to process cluster status updates.
cluster_status_q = queue.Queue()  # type: queue.Queue[Union[None, Tuple[str, str, str]]]


class RayCluster(object):
    """Manages an autoscaling Ray cluster.

    Attributes:
        config: Autoscaling configuration dict.
        subprocess: The subprocess used to create, update, and monitor the
        Ray cluster.
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.name = self.config["cluster_name"]
        self.namespace = self.config["provider"]["namespace"]

        # Make directory for configs of clusters in the namespace,
        # if the directory doesn't exist already.
        namespace_dir = operator_utils.namespace_dir(self.namespace)
        os.makedirs(namespace_dir, exist_ok=True)

        self.config_path = operator_utils.config_path(
            cluster_namespace=self.namespace, cluster_name=self.name)

        # Monitor subprocess
        self.subprocess = None  # type: Optional[mp.Process]
        # Monitor logs for this cluster will be prefixed by the monitor
        # subprocess name:
        self.subprocess_name = ",".join([self.name, self.namespace])
        self.monitor_stop_event = mp.Event()
        self.setup_logging()

    def create_or_update(self, restart_ray: bool = False) -> None:
        """ Create/update the Ray Cluster and run the monitoring loop, all in a
        subprocess.

        The main function of the Operator is managing the
        subprocesses started by this method.

        Args:
            restart_ray: If True, restarts Ray to recover from failure.
        """
        self.do_in_subprocess(self._create_or_update, args=(restart_ray, ))

    def _create_or_update(self, restart_ray: bool = False) -> None:
        try:
            self.start_head(restart_ray=restart_ray)
            self.start_monitor()
        except Exception:
            # Report failed autoscaler status to trigger cluster restart.
            cluster_status_q.put((self.name, self.namespace, STATUS_AUTOSCALING_EXCEPTION))
            # `status_handling_loop` will increment the
            # `status.AutoscalerRetries` of the CR. A restart will trigger
            # at the subsequent "MODIFIED" event.
            raise

    def start_head(self, restart_ray: bool = False) -> None:
        self.write_config()
        # Don't restart Ray on head unless recovering from failure.
        no_restart = not restart_ray
        # Create or update cluster head and record config side effects.
        self.config = commands.create_or_update_cluster(
            self.config_path,
            override_min_workers=None,
            override_max_workers=None,
            no_restart=no_restart,
            restart_only=False,
            yes=True,
            no_config_cache=True,
            no_monitor_on_head=True)
        # Write the resulting config for use by the autoscaling monitor:
        self.write_config()

    def start_monitor(self) -> None:
        """Runs the autoscaling monitor."""
        ray_head_pod_ip = commands.get_head_node_ip(self.config_path)
        port = operator_utils.infer_head_port(self.config)
        redis_address = services.address(ray_head_pod_ip, port)
        self.mtr = monitor.Monitor(
            redis_address=redis_address,
            autoscaling_config=self.config_path,
            redis_password=ray_constants.REDIS_DEFAULT_PASSWORD,
            prefix_cluster_info=True,
            stop_event=self.monitor_stop_event)
        self.mtr.run()

    def do_in_subprocess(self, f: Callable[[], None], args: Tuple) -> None:
        # First stop the subprocess if it's alive
        self.clean_up_subprocess()
        # Reinstantiate process with f as target and start.
        self.subprocess = mp.Process(
            name=self.subprocess_name, target=f, args=args, daemon=True)
        self.subprocess.start()

    def clean_up_subprocess(self):
        """
        Clean up the monitor process.

        Executed when CR for this cluster is "DELETED".
        Executed when Autoscaling monitor is restarted.
        """
        if self.subprocess and self.subprocess.is_alive():
            # Triggers graceful stop of the monitor loop.
            self.monitor_stop_event.set()
            self.subprocess.join()
            # Clears the event for subsequent runs of the monitor.
            self.monitor_stop_event.clear()

    def clean_up(self) -> None:
        """Executed when the CR for this cluster is "DELETED".

        The key thing is to end the monitoring subprocess.
        """
        self.clean_up_subprocess()
        self.clean_up_logging()
        self.delete_config()

    def setup_logging(self) -> None:
        """Add a log handler which appends the name and namespace of this
        cluster to the cluster's monitor logs.
        """
        self.handler = logging.StreamHandler()
        # Filter by subprocess name to get this cluster's monitor logs.
        self.handler.addFilter(
            lambda rec: rec.processName == self.subprocess_name)
        # Lines start with "<cluster name>,<cluster namespace>:"
        logging_format = ":".join(
            [self.subprocess_name, ray_constants.LOGGER_FORMAT])
        self.handler.setFormatter(logging.Formatter(logging_format))
        operator_utils.root_logger.addHandler(self.handler)

    def clean_up_logging(self) -> None:
        operator_utils.root_logger.removeHandler(self.handler)

    def set_config(self, config: Dict[str, Any]) -> None:
        self.config = config

    def write_config(self) -> None:
        """Write config to disk for use by the autoscaling monitor."""
        with open(self.config_path, "w") as file:
            yaml.dump(self.config, file)

    def delete_config(self) -> None:
        try:
            os.remove(self.config_path)
        except OSError:
            log_prefix = ",".join([self.name, self.namespace])
            logger.warning(f"{log_prefix}: config path does not exist {self.config_path}")


# Maps ray cluster (name, namespace) pairs to RayCluster python objects.
# TODO: decouple monitoring background thread into a kopf.daemon and move this into
#  the memo state.
ray_clusters = {}  # type: Dict[Tuple[str, str], RayCluster]


@kopf.on.startup()
def start_background_worker(memo: kopf.Memo, **_):
    memo.status_handler = threading.Thread(target=status_handling_loop, daemon=True)
    memo.status_handler.start()


@kopf.on.cleanup()
def stop_background_worker(memo: kopf.Memo, **_):
    cluster_status_q.put(None)
    memo.status_handler.join()


def status_handling_loop(queue: queue.Queue):
    while True:
        item = queue.get()
        if item is None:
            break

        cluster_name, cluster_namespace, phase = item
        try:
            operator_utils.set_status(cluster_name, cluster_namespace, phase)
        except Exception:
            log_prefix = ",".join([cluster_name, cluster_namespace])
            logger.exception(f"{log_prefix}: Error setting RayCluster status.")


@kopf.on.resume('rayclusters')
@kopf.on.create('rayclusters')
def create_fn(body, name, namespace, logger, **kwargs):
    cluster_config = operator_utils.cr_to_config(body)
    cluster_identifier = (name, namespace)
    log_prefix = ",".join(cluster_identifier)

    operator_utils.check_redis_password_not_specified(
        cluster_config, cluster_identifier)

    ray_cluster = RayCluster(cluster_config)
    ray_clusters[cluster_identifier] = ray_cluster
    cluster_status_q.put((name, namespace, STATUS_UPDATING))

    # Launch a the Ray cluster by SSHing into the pod and running
    # the initialization commands. This will not restart the cluster
    # unless there was a failure.
    logger.info(f"{log_prefix}: Launching cluster.")
    ray_cluster.create_or_update()
    cluster_status_q.put((name, namespace, STATUS_RUNNING))


@kopf.on.update('rayclusters')
def update_fn(body, old, new, name, namespace, **kwargs):
    cluster_config = operator_utils.cr_to_config(body)
    cluster_identifier = (name, namespace)

    # Check metadata.generation to determine if there's a spec change.
    old_generation = old["metadata"]["generation"]
    current_generation = new["metadata"]["generation"]
    # Check metadata.labels.autoscalerRetries to see if we need to restart
    # Ray processes.
    old_autoscaler_retries = old.get("status", {}).get(AUTOSCALER_RETRIES_FIELD, 0)
    autoscaler_retries = new.get("status", {}).get(AUTOSCALER_RETRIES_FIELD, 0)

    # True if there's been a chamge to the spec of the custom resource,
    # triggering an increment of metadata.generation:
    spec_changed = current_generation > old_generation
    # True if monitor has failed, triggering an increment of
    # status.autoscalerRetries:
    ray_restart_required = (autoscaler_retries >
                            old_autoscaler_retries)

    # Update if there's been a change to the spec or if we're attempting
    # recovery from autoscaler failure.
    if spec_changed or ray_restart_required:
        ray_cluster = ray_clusters.get(cluster_identifier)
        if ray_cluster is None:
            ray_cluster = RayCluster(cluster_config)
            ray_clusters[cluster_identifier] = ray_cluster

        cluster_status_q.put((name, namespace, STATUS_UPDATING))

        # Clean up the previous cluster monitor processes to prevent running multiple
        # overlapping background threads
        ray_cluster.clean_up_subprocess()

        # Update the config and restart the Ray processes if there's been a failure
        ray_cluster.set_config(cluster_config)
        ray_cluster.create_or_update(restart_ray=ray_restart_required)
        cluster_status_q.put((name, namespace, STATUS_RUNNING))


@kopf.on.delete('rayclusters')
def delete_fn(name, namespace, **kwargs):
    cluster_identifier = (name, namespace)
    ray_cluster = ray_clusters.get(cluster_identifier)
    if ray_cluster is None:
        return

    ray_cluster.clean_up()
    del ray_clusters[cluster_identifier]
