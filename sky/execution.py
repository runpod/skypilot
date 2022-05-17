"""Execution layer: resource provisioner + task launcher.

Usage:

   >> sky.launch(planned_dag)

Current resource privisioners:

  - Ray autoscaler

Current task launcher:

  - ray exec + each task's commands
"""
import enum
import hashlib
import os
import sys
import time
import traceback
from typing import Any, List, Optional, Union, Tuple

import filelock

import sky
from sky import backends
from sky import global_user_state
from sky import optimizer
from sky import sky_logging
from sky import spot
from sky.backends import backend_utils
from sky.backends import timeline
from sky.backends.cloud_vm_ray_backend import _LOCK_FILENAME
from sky.spot import spot_utils

logger = sky_logging.init_logger(__name__)

OptimizeTarget = optimizer.OptimizeTarget


class Stage(enum.Enum):
    """Stages for a run of a sky.Task."""
    # TODO: rename actual methods to be consistent.
    OPTIMIZE = enum.auto()
    PROVISION = enum.auto()
    SYNC_WORKDIR = enum.auto()
    SYNC_FILE_MOUNTS = enum.auto()
    SETUP = enum.auto()
    PRE_EXEC = enum.auto()
    EXEC = enum.auto()
    TEARDOWN = enum.auto()


def _execute(dag: sky.Dag,
             dryrun: bool = False,
             teardown: bool = False,
             stream_logs: bool = True,
             handle: Any = None,
             backend: Optional[backends.Backend] = None,
             optimize_target: OptimizeTarget = OptimizeTarget.COST,
             stages: Optional[List[Stage]] = None,
             cluster_name: Optional[str] = None,
             detach_run: bool = False,
             idle_minutes_to_autostop: Optional[int] = None) -> None:
    """Runs a DAG.

    If the DAG has not been optimized yet, this will call sky.optimize() for
    the caller.

    Args:
      dag: sky.Dag.
      dryrun: bool; if True, only print the provision info (e.g., cluster
        yaml).
      teardown: bool; whether to teardown the launched resources after
        execution.
      stream_logs: bool; whether to stream all tasks' outputs to the client.
      handle: Any; if provided, execution will use an existing backend cluster
        handle instead of provisioning a new one.
      backend: Backend; backend to use for executing the tasks. Defaults to
        CloudVmRayBackend()
      optimize_target: OptimizeTarget; the dag optimization metric, e.g.
        OptimizeTarget.COST.
      stages: List of stages to run.  If None, run the whole life cycle of
        execution; otherwise, just the specified stages.  Used for `sky exec`
        skipping all setup steps.
      cluster_name: Name of the cluster to create/reuse.  If None,
        auto-generate a name.
      detach_run: bool; whether to detach the process after the job submitted.
      autostop_idle_minutes: int; if provided, the cluster will be set to
        autostop after this many minutes of idleness.
    """
    assert len(dag) == 1, f'Sky assumes 1 task for now. {dag}'
    task = dag.tasks[0]

    if task.need_spot_recovery:
        logger.error('Spot recovery is specified in the task. To launch the '
                     'managed spot job, please use: sky spot launch')
        sys.exit(1)

    cluster_exists = False
    if cluster_name is not None:
        existing_handle = global_user_state.get_handle_from_cluster_name(
            cluster_name)
        cluster_exists = existing_handle is not None

    backend = backend if backend is not None else backends.CloudVmRayBackend()
    if not isinstance(backend, backends.CloudVmRayBackend
                     ) and idle_minutes_to_autostop is not None:
        # TODO(zhwu): Autostop is not supported for non-CloudVmRayBackend.
        raise ValueError(
            f'Backend {backend.NAME} does not support autostop, please try '
            f'{backends.CloudVmRayBackend.NAME}')

    if not cluster_exists and (stages is None or Stage.OPTIMIZE in stages):
        if task.best_resources is None:
            # TODO: fix this for the situation where number of requested
            # accelerators is not an integer.
            if isinstance(backend, backends.CloudVmRayBackend):
                # TODO: adding this check because docker backend on a
                # no-credential machine should not enter optimize(), which
                # would directly error out ('No cloud is enabled...').  Fix by
                # moving `sky check` checks out of optimize()?
                dag = sky.optimize(dag, minimize=optimize_target)
                task = dag.tasks[0]  # Keep: dag may have been deep-copied.
                assert task.best_resources is not None, task

    backend.register_info(dag=dag, optimize_target=optimize_target)

    if task.storage_mounts is not None:
        # Optimizer should eventually choose where to store bucket
        task.add_storage_mounts()

    status_printed = False
    try:
        if stages is None or Stage.PROVISION in stages:
            if handle is None:
                handle = backend.provision(task,
                                           task.best_resources,
                                           dryrun=dryrun,
                                           stream_logs=stream_logs,
                                           cluster_name=cluster_name)

        if dryrun:
            logger.info('Dry run finished.')
            return

        if stages is None or Stage.SYNC_WORKDIR in stages:
            if task.workdir is not None:
                backend.sync_workdir(handle, task.workdir)

        if stages is None or Stage.SYNC_FILE_MOUNTS in stages:
            backend.sync_file_mounts(handle, task.file_mounts,
                                     task.storage_mounts)

        if stages is None or Stage.SETUP in stages:
            backend.setup(handle, task)

        if stages is None or Stage.PRE_EXEC in stages:
            if idle_minutes_to_autostop is not None:
                backend.set_autostop(handle, idle_minutes_to_autostop)

        if stages is None or Stage.EXEC in stages:
            try:
                global_user_state.update_last_use(handle.get_cluster_name())
                backend.execute(handle, task, detach_run)
            finally:
                # Enables post_execute() to be run after KeyboardInterrupt.
                backend.post_execute(handle, teardown)

        if stages is None or Stage.TEARDOWN in stages:
            if teardown:
                backend.teardown_ephemeral_storage(task)
                backend.teardown(handle)
    except Exception:  # pylint: disable=broad-except
        # UX: print live clusters to make users aware (to save costs).
        # Shorter stacktrace than raise e (e.g., no cli stuff).
        traceback.print_exc()
        print()
        if cluster_name == spot.SPOT_CONTROLLER_NAME:
            # For spot controller task, it requires a while to have the
            # managed spot status shown in the status table.
            time.sleep(0.5)
            backends.backend_utils.run('sky spot status')
        else:
            backends.backend_utils.run('sky status')
        print('\x1b[?25h', end='')  # Show cursor.
        status_printed = True
        sys.exit(1)
    finally:
        if not status_printed:
            # Needed because this finally doesn't always get executed on errors.
            if cluster_name == spot.SPOT_CONTROLLER_NAME:
                # For spot controller task, it requires a while to have the
                # managed spot status shown in the status table.
                time.sleep(0.5)
                backends.backend_utils.run('sky spot status')
            else:
                backends.backend_utils.run('sky status')
            print('\x1b[?25h', end='')  # Show cursor.


@timeline.event('execution.launch')
def launch(dag: sky.Dag,
           dryrun: bool = False,
           teardown: bool = False,
           stream_logs: bool = True,
           backend: Optional[backends.Backend] = None,
           optimize_target: OptimizeTarget = OptimizeTarget.COST,
           cluster_name: Optional[str] = None,
           detach_run: bool = False,
           idle_minutes_to_autostop: Optional[int] = None,
           is_spot_controller_task: bool = False) -> None:
    if not is_spot_controller_task:
        backend_utils.check_cluster_name_not_reserved(
            cluster_name, operation_str='sky.launch')
    return _execute(dag=dag,
                    dryrun=dryrun,
                    teardown=teardown,
                    stream_logs=stream_logs,
                    handle=None,
                    backend=backend,
                    optimize_target=optimize_target,
                    cluster_name=cluster_name,
                    detach_run=detach_run,
                    idle_minutes_to_autostop=idle_minutes_to_autostop)


@timeline.event
def exec(  # pylint: disable=redefined-builtin
    dag: sky.Dag,
    cluster_name: str,
    dryrun: bool = False,
    teardown: bool = False,
    stream_logs: bool = True,
    backend: Optional[backends.Backend] = None,
    optimize_target: OptimizeTarget = OptimizeTarget.COST,
    detach_run: bool = False,
) -> None:
    backend_utils.check_cluster_name_not_reserved(cluster_name,
                                                  operation_str='sky.exec')

    status, handle = backend_utils.refresh_cluster_status_handle(cluster_name)
    if handle is None:
        logger.error(f'Cluster {cluster_name!r} not found.  '
                     'Use `sky launch` to provision first.')
        sys.exit(1)
    if status != global_user_state.ClusterStatus.UP:
        logger.error(f'Cluster {cluster_name!r} is not up.  '
                     'Use `sky status` to check the status.')
        sys.exit(1)
    _execute(dag=dag,
             dryrun=dryrun,
             teardown=teardown,
             stream_logs=stream_logs,
             handle=handle,
             backend=backend,
             optimize_target=optimize_target,
             stages=[
                 Stage.SYNC_WORKDIR,
                 Stage.EXEC,
             ],
             cluster_name=cluster_name,
             detach_run=detach_run)


@timeline.event
def exec_or_launch(
        dag: sky.Dag,
        dryrun: bool = False,
        teardown: bool = False,
        stream_logs: bool = True,
        backend: Optional[backends.Backend] = None,
        optimize_target: OptimizeTarget = OptimizeTarget.COST,
        cluster_name: Optional[str] = None,
        detach_run: bool = False,
        idle_minutes_to_autostop: Optional[int] = None,
        is_spot_controller_task: bool = False) -> None:
    """Try execute on the cluster. If the cluster is not up,
    then launch the cluster. This helps improving efficiency significantly."""

    if not is_spot_controller_task:
        backend_utils.check_cluster_name_not_reserved(
            cluster_name, operation_str='sky.exec_or_launch')

    lock_path = os.path.expanduser(
        _LOCK_FILENAME.format(cluster_name) + '.exec_or_launch')

    # TODO(mraheja): remove pylint disabling when filelock version updated
    # pylint: disable=abstract-class-instantiated
    lock = filelock.FileLock(lock_path)
    try:
        lock.acquire()
        status, handle = backend_utils.refresh_cluster_status_handle(
            cluster_name)
        if handle is not None and status == global_user_state.ClusterStatus.UP:
            lock.release()
            return _execute(dag=dag,
                            dryrun=dryrun,
                            teardown=teardown,
                            stream_logs=stream_logs,
                            handle=handle,
                            backend=backend,
                            optimize_target=optimize_target,
                            stages=[
                                Stage.SYNC_WORKDIR,
                                Stage.EXEC,
                                Stage.SYNC_FILE_MOUNTS,
                            ],
                            cluster_name=cluster_name,
                            detach_run=detach_run)
        else:
            return launch(dag=dag,
                          dryrun=dryrun,
                          teardown=teardown,
                          stream_logs=stream_logs,
                          backend=backend,
                          optimize_target=optimize_target,
                          cluster_name=cluster_name,
                          detach_run=detach_run,
                          idle_minutes_to_autostop=idle_minutes_to_autostop,
                          is_spot_controller_task=is_spot_controller_task)
    finally:
        # file lock can be released multiple times, so we just release
        # it here anyway
        lock.release()


def exec_spot(user_yaml_path: str,
              cluster_name: Optional[str] = None,
              detach_run: bool = False):
    backend = backends.CloudVmRayBackend()

    def _get_queue(index: int):
        queue_size = spot_utils.OVERSUBSCRIPTION_RATIO * 8
        name = spot.SPOT_CONTROLLER_NAME + f'-{index}'
        return backend_utils.FilelockQueue(
            os.path.expanduser(_LOCK_FILENAME.format(name)) + '.queue',
            queue_size)

    controller_index = 1

    while True:
        print(f">>>>>>>>> controller_index = {controller_index}")
        queue = _get_queue(controller_index)

        if queue.enter():
            try:
                if controller_index == 1:
                    controller_name = spot.SPOT_CONTROLLER_NAME
                else:
                    controller_name = spot.SPOT_CONTROLLER_NAME + f'-{controller_index}'
                yaml_path = backend_utils.fill_template(
                    spot.SPOT_CONTROLLER_TEMPLATE, {
                        'user_yaml_path': user_yaml_path,
                        'spot_controller': controller_name,
                        'cluster_name': cluster_name,
                        'sky_remote_path': backend_utils.SKY_REMOTE_PATH,
                    },
                    output_prefix=spot.SPOT_CONTROLLER_YAML_PREFIX)
                with sky.Dag() as dag:
                    task = sky.Task.from_yaml(yaml_path)
                    assert len(task.resources) == 1

                exec_or_launch(
                    dag,
                    stream_logs=True,
                    cluster_name=controller_name,
                    detach_run=detach_run,
                    backend=backend,
                    idle_minutes_to_autostop=spot.
                    SPOT_CONTROLLER_IDLE_MINUTES_TO_AUTOSTOP,
                    is_spot_controller_task=True)
                return
            finally:
                queue.exit()
        else:
            child = hashlib.sha1(
                f'{cluster_name}_{controller_index}'.encode()).digest()[-1] % 2
            controller_index = controller_index * 2 + child
