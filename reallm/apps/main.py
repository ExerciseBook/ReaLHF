from typing import Dict, List, Optional
import argparse
import getpass
import os
import re

from reallm.scheduler.client import JobException, JobState
import reallm.api.core.system_api as config_package
import reallm.base.constants as constants
import reallm.base.logging as logging
import reallm.base.name_resolve as name_resolve
import reallm.base.names as names
import reallm.scheduler.client as sched_client
import reallm.system as system

logger = logging.getLogger("main", "system")

CONTROLLER_TIME_LIMIT = None
TRACE_TIMEOUT = 360  # Should be larger than TRACER_SAVE_INTERVAL_SECONDS defined in system/worker_base.py


def scheduler_mode(mode: str) -> str:
    if mode == "ray" or mode == "slurm":
        return "slurm"
    elif "local" in mode:
        return "local"


def _submit_workers(
    sched: sched_client.SchedulerClient,
    expr_name: str,
    trial_name: str,
    debug: bool,
    worker_type: str,
    scheduling_configs: List[config_package.TasksGroup],
    environs: Dict[str, str],
    image_name: Optional[str] = None,
    use_ray_cluster: bool = False,
) -> List[str]:
    if len(scheduling_configs) == 0:
        return []

    scheduled_jobs = []
    for sch_cfg in scheduling_configs:
        job_environs = {**environs, **sch_cfg.scheduling.env_vars}
        if use_ray_cluster:
            cmd = sched_client.ray_cluster_cmd(
                expr_name,
                trial_name,
                worker_type=worker_type,
            )
        else:
            cmd = sched_client.remote_worker_cmd(expr_name, trial_name, debug, worker_type)

        logger.debug(f"Scheduling worker {worker_type}, {scheduling_configs}")

        nodelist = sch_cfg.scheduling.nodelist
        exclude = sch_cfg.scheduling.exclude
        node_type = sch_cfg.scheduling.node_type
        container_image = image_name or sch_cfg.scheduling.container_image
        if use_ray_cluster:
            worker_type = f"rc_{worker_type}"

        scheduled_jobs.append(
            sched.submit_array(
                worker_type=worker_type,
                cmd=cmd,
                count=sch_cfg.count,
                cpu=sch_cfg.scheduling.cpu,
                gpu=sch_cfg.scheduling.gpu,
                gpu_type=sch_cfg.scheduling.gpu_type,
                mem=sch_cfg.scheduling.mem,
                container_image=container_image,
                node_type=node_type,
                nodelist=nodelist,
                exclude=exclude,
                env_vars=job_environs,
                hostfile=True,
                multiprog=True,
                begin=sch_cfg.scheduling.begin,
                deadline=sch_cfg.scheduling.deadline,
                time_limit=sch_cfg.scheduling.time_limit,
            ),)
    return scheduled_jobs


def get_repo_path():
    file_path = os.path.abspath(__file__)
    reallm_path = os.path.dirname(os.path.dirname(file_path))
    repo_path = os.path.dirname(reallm_path)
    return repo_path


def main_start(args, recover_count: int = 0):
    if args.mode == "ray" and args.image_name is None:
        raise ValueError("--image_name must be specified when using ray cluster. "
                         "This is becuase ray cluster requires all workers to have "
                         "the same version of Python and ray.")
    if args.mode == "local":
        assert (args.recover_mode == "disabled"), "Recover mode is not supported for local runs!"
    # Use search cache for recover runs
    force_allocation_use_cache = (recover_count > 1
                                  or args.recover_mode == "resume") and args.allocation_mode == "search"

    args.ignore_worker_error = (args.ignore_worker_error and args.recover_mode == "disabled")

    trial_name = args.trial_name or f"test-{getpass.getuser()}"
    expr_name = args.experiment_name
    if recover_count == 0:
        constants.set_experiment_trial_names(args.experiment_name, args.trial_name)

    repo_path = get_repo_path()

    is_recover_run = (args.recover_mode == "auto" and recover_count > 0) or args.recover_mode == "resume"
    save_recover_states = args.recover_mode != "disabled"

    BASE_ENVIRONS = {
        "PYTHONPATH": repo_path,
        "REAL_PACKAGE_PATH": repo_path,
        "WANDB_MODE": args.wandb_mode,
        "REAL_MODE": args.mode.upper(),
        "REAL_TRACE": os.getenv("REAL_TRACE", "0"),
        "IS_REMOTE": "1",
        # identify whether this run is automatically recovering the last failed run
        "RECOVER_RUN": "1" if is_recover_run else "0",
        "SAVE_RECOVER_STATES": "1" if save_recover_states else "0",
    }

    os.environ["IS_REMOTE"] = "0" if not force_allocation_use_cache else "1"
    os.environ["REAL_PACKAGE_PATH"] = repo_path

    experiment = config_package.make_experiment(args.experiment_name)
    if args.allocation_mode == "search":
        experiment._search()

    sched = sched_client.make(mode=scheduler_mode(args.mode), expr_name=expr_name, trial_name=trial_name)

    setup = experiment.scheduling_setup()

    logger.info(f"Resetting name resolving repo...")

    if args.remote_reset:
        sched.submit(
            "setup",
            cmd=sched_client.setup_cmd(expr_name, trial_name, args.debug),
            env_vars=BASE_ENVIRONS,
            container_image=args.image_name or setup.controller_image,
            multiprog=False,
            hostfile=False,
        )
        try:
            sched.wait(timeout=3600, update=True)
        except Exception as e:
            logger.warning(f"Resetting name resolving repo failed.")
            raise e
    else:
        try:
            name_resolve.clear_subtree(
                names.trial_root(experiment_name=args.experiment_name, trial_name=args.trial_name))
        except Exception as e:
            logger.warning(f"Resetting name resolving repo failed.")
            raise e
    logger.info(f"Resetting name resolving repo... Done.")

    logger.info(f"Running configuration: {experiment.__class__.__name__}")

    # Schedule controller
    if args.mode == "ray":
        controller_type = "ray"
    elif args.mode == "local_ray":
        controller_type = "local_ray"
    else:
        controller_type = "zmq"
    # For local_ray mode, the controller will start all remote workers.
    sched.submit_array(
        worker_type="ctl",
        cmd=sched_client.control_cmd(
            expr_name,
            trial_name,
            args.debug,
            args.ignore_worker_error,
            controller_type,
        ),
        count=1,
        cpu=1,
        gpu=0,
        mem=1024,
        env_vars=BASE_ENVIRONS,
        container_image=args.image_name or setup.controller_image,
        time_limit=CONTROLLER_TIME_LIMIT,
    )

    if args.mode != "local_ray":
        workers_configs = ((k, getattr(setup, k)) for k in system.WORKER_TYPES)

        for name, scheduling_setup in workers_configs:
            if not isinstance(scheduling_setup, list):
                scheduling_setup = [scheduling_setup]
            # For local or slurm mode, launch all workers.
            # For ray mode, launch the ray cluster for all workers via slurm.
            _submit_workers(
                sched,
                expr_name,
                trial_name,
                args.debug,
                name,
                scheduling_setup,
                BASE_ENVIRONS,
                args.image_name,
                use_ray_cluster=(args.mode == "ray"),
            )

    timeout = (None if os.getenv("REAL_TRACE", "0") == "0" else TRACE_TIMEOUT)  # run 5 mins to collect trace
    try:
        sched.wait(
            check_status=(
                JobState.CANCELLED,
                JobState.FAILED,
                JobState.NOT_FOUND,
                JobState.COMPLETED,
            ),
            remove_status=(),
            timeout=timeout,
        )
    except (KeyboardInterrupt, JobException, TimeoutError) as e:
        if os.getenv("REAL_TRACE", "0") != "0" and isinstance(e, TimeoutError):
            s = "#" * 30 + "  Trace complete. Killing all processes...  " + "#" * 30
            logger.info("\n" + "#" * len(s) + "\n" + s + "\n" + "#" * len(s))

        recover_states = [JobState.CANCELLED, JobState.FAILED, JobState.NOT_FOUND]
        reason = e.reason if isinstance(e, JobException) else None
        recover_this = (args.recover_mode == "auto" and recover_count < args.recover_retries)
        recover_this = recover_this and reason in recover_states

        # FIXME: in recover mode, this will interrupt saving exit
        #        hook of the error worker as well, fix this by modifying stop_all method!
        sched.stop_all("SIGINT" if (recover_this or args.recover_mode == "save") else "SIGKILL")
        if recover_this:
            logger.warning(f"Recovering from error {e}. Recover count: {recover_count+1}, "
                           f"total recover count {args.recover_retries}")
            main_start(args, recover_count=recover_count + 1)
        else:
            raise e


def main_stop(args):
    sched = sched_client.make(
        mode=scheduler_mode(args.mode),
        expr_name=args.experiment_name,
        trial_name=args.trial_name,
    )
    sched.find_all()
    sched.stop_all()


def main_find_config(args):
    exp_names = [x for x in config_package.ALL_EXPERIMENT_CLASSES if re.match(args.regex, x)]
    if len(exp_names) == 0:
        print("No matched experiment names.")
    if len(exp_names) > 20:
        response = input(f"Found {len(exp_names)} experiments, list all?(y/n)")
        if response != "y":
            return
    for exp_name in exp_names:
        print(exp_name)


def main_profile_layers(args):
    from reallm.api.core.model_api import ModelFamily

    _main_profile_layers(ModelFamily(args.model_class, args.model_size, args.is_critic), args.model_path)


def _main_profile_layers(model_family, model_path):
    from reallm.api.core.model_api import ModelFamily
    from reallm.base.slurm_utils import check_slurm_availability
    from reallm.base.testing import clear_name_resolve

    expr_name = trial_name = "profile"
    cmd = (f"python3 -m reallm.apps.profile_layers --expr_name {expr_name} --trial_name {trial_name} "
           f"--model_path {model_path} --model_name {model_family} ")

    if check_slurm_availability():
        repo_path = get_repo_path()

        from reallm.api.core.system_api import _LLM_ENVVARS

        BASE_ENVIRONS = {
            "PYTHONPATH": repo_path,
            "REAL_PACKAGE_PATH": repo_path,
            "WANDB_MODE": "disabled",
            "DLLM_MODE": "SLURM",
            "DLLM_TRACE": "0",
            **_LLM_ENVVARS,
        }
        clear_name_resolve(expr_name, trial_name)
        sched = sched_client.make(mode="slurm", expr_name=expr_name, trial_name=trial_name)
        print(f"Profiling {model_family} layers, model path {model_path}, "
              f"cmd {cmd}")
        sched.submit_array(
            worker_type="profile_layer",
            cmd=cmd,
            count=1,
            cpu=64,
            gpu=8,
            gpu_type="tesla",
            mem=500000,
            env_vars=BASE_ENVIRONS,
            container_image=config_package._LLM_GPU_IMAGE,
        )

        try:
            sched.wait(timeout=None)
        except (KeyboardInterrupt, sched_client.JobException, TimeoutError) as e:
            sched.stop_all()
            raise e
    else:
        try:
            print(f"Profiling {model_family} layers, model path {model_path}, "
                  f"cmd {cmd}")
            clear_name_resolve(expr_name, trial_name)
            os.system(cmd)
        except (KeyboardInterrupt, sched_client.JobException, TimeoutError) as e:
            raise e


def main():
    parser = argparse.ArgumentParser(prog="distributed_llm")
    subparsers = parser.add_subparsers(dest="cmd", help="sub-command help")
    subparsers.required = True

    subparser = subparsers.add_parser("start", help="starts an experiment")
    subparser.add_argument(
        "--experiment_name",
        "-e",
        type=str,
        required=True,
        help="name of the experiment",
    )
    subparser.add_argument(
        "--trial_name",
        "-f",
        type=str,
        default=None,
        help="trial name; by default uses '<USER>-test'",
    )
    subparser.add_argument("--mode", default="slurm", choices=["local", "slurm", "ray", "local_ray"])
    subparser.add_argument("--partition", default="dev", help="slurm partition to schedule the trial")
    subparser.add_argument(
        "--wandb_mode",
        type=str,
        default="disabled",
        choices=["online", "offline", "disabled"],
    )
    subparser.add_argument(
        "--image_name",
        type=str,
        required=False,
        default=None,
        help="if specified, all workers will use this image. Useful in CI/CD pipeline.",
    )
    subparser.add_argument("--ignore_worker_error", action="store_true")
    subparser.add_argument(
        "--debug",
        action="store_true",
        help="If True, activate all assertions in the code.",
    )
    subparser.add_argument(
        "--remote_reset",
        action="store_true",
        help="If True, reset name resolve repo remotely in computation nodes. Otherwise reset locally.",
    )
    subparser.add_argument(
        "--recover_mode",
        required=False,
        default="disabled",
        choices=["disabled", "auto", "save", "resume"],
        help="Recover mode, 'auto': automatically recover the last failed run; "
        "'save': save recover states if any error occurs; "
        "'resume': resume from saved recover states and save states if fail again; "
        "'disabled': do nothing when error occurs. ",
    )
    subparser.add_argument(
        "--recover_retries",
        type=int,
        required=False,
        default=1,
        help="Total number of trials for the system to recover automatically when a worker fails. "
        "Only effective when recover_mode is 'auto'.",
    )
    subparser.set_defaults(ignore_worker_error=False)
    subparser.set_defaults(func=main_start)

    subparser = subparsers.add_parser("stop", help="stops an experiment. only slurm experiment is supported.")
    subparser.add_argument(
        "--experiment_name",
        "-e",
        type=str,
        required=True,
        help="name of the experiment",
    )
    subparser.add_argument("--trial_name", "-f", type=str, required=True, help="name of the trial")
    subparser.add_argument("--mode", default="slurm", choices=["local", "slurm", "ray", "local_ray"])
    subparser.set_defaults(func=main_stop)

    subparser = subparsers.add_parser("find_config",
                                      help="find configuration by matching regular expression.")
    subparser.add_argument("--regex", "-r", type=str, required=True)
    subparser.set_defaults(func=main_find_config)

    subparser = subparsers.add_parser("profile_layers", help="profile layers of a model.")
    subparser.add_argument("--model_class", type=str, required=True)
    subparser.add_argument("--model_size", type=int, required=True)
    subparser.add_argument("--is_critic", action="store_true")
    subparser.add_argument("--model_path", type=str, required=True)
    subparser.set_defaults(func=main_profile_layers)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
