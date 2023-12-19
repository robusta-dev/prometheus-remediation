import logging
from typing import List, Tuple, Optional

import hikaru
from hikaru.model.rel_1_26 import Container, EnvVar, Job, JobSpec, JobStatus, ObjectMeta, PodSpec, PodTemplateSpec, EnvVarSource, SecretKeySelector

from robusta.api import (
    ActionParams,
    EventEnricherParams,
    FileBlock,
    JobEvent,
    LogEnricherParams,
    MarkdownBlock,
    PodContainer,
    PrometheusKubernetesAlert,
    RegexReplacementStyle,
    RobustaJob,
    SlackAnnotations,
    TableBlock,
    action,
    get_job_latest_pod,
    get_resource_events_table,
    to_kubernetes_name,
)

from pydantic import BaseModel, SecretStr, validator

from robusta.integrations.kubernetes.api_client_utils import (
    SUCCEEDED_STATE,
    exec_shell_command,
    get_pod_logs,
    prepare_pod_command,
    to_kubernetes_name,
    upload_file,
    wait_for_pod_status,
    wait_until_job_complete,
)
# class EnvVar(name: str, value: Optional[str]=None, valueFrom: Optional["EnvVarSource"]=None)


class JobParams(ActionParams):
    """
    :var image: The job image.
    :var command: The job command as array of strings
    :var name: Custom name for the job and job container.
    :var namespace: The created job namespace.
    :var service_account: Job pod service account. If omitted, default is used.
    :var restart_policy: Job container restart policy
    :var job_ttl_after_finished: Delete finished job ttl (seconds). If omitted, jobs will not be deleted automatically.
    :var notify: Add a notification for creating the job.
    :var wait_for_completion: Wait for the job to complete and attach it's output. Only relevant when notify=true.
    :var completion_timeout: Maximum seconds to wait for job to complete. Only relevant when wait_for_completion=true.
    :var backoff_limit: Specifies the number of retries before marking this job failed. Defaults to 6
    :var active_deadline_seconds: Specifies the duration in seconds relative to the startTime
        that the job may be active before the system tries to terminate it; value must be
        positive integer

    :example command: ["perl",  "-Mbignum=bpi", "-wle", "print bpi(2000)"]
    """

    image: str
    command: List[str]
    name: str = "robusta-action-job"
    namespace: str = "default"
    service_account: str = None  # type: ignore
    restart_policy: str = "OnFailure"
    job_ttl_after_finished: int = 120  # type: ignore
    notify: bool = False
    wait_for_completion: bool = True
    completion_timeout: int = 300
    backoff_limit: int = None  # type: ignore
    active_deadline_seconds: int = None  # type: ignore
    env_vars: Optional[List[EnvVar]] = None



def __get_alert_env_vars(event: PrometheusKubernetesAlert, params: JobParams) -> List[EnvVar]:
    alert_subject = event.get_alert_subject()
    alert_env_vars = [
        EnvVar(name="ALERT_NAME", value=event.alert_name),
        EnvVar(name="ALERT_STATUS", value=event.alert.status),
        EnvVar(name="ALERT_OBJ_KIND", value=alert_subject.subject_type.value),
        EnvVar(name="ALERT_OBJ_NAME", value=alert_subject.name),
    ]
    
    if alert_subject.namespace:
        alert_env_vars.append(EnvVar(name="ALERT_OBJ_NAMESPACE", value=alert_subject.namespace))
    if alert_subject.node:
        alert_env_vars.append(EnvVar(name="ALERT_OBJ_NODE", value=alert_subject.node))
    if params.env_var != None:
        alert_env_vars.extend(params.env_vars)

    label_vars = [EnvVar(name=f"ALERT_LABEL_{k.upper()}", value=v) for k,v in event.alert.labels.items()]
    alert_env_vars += label_vars

    return alert_env_vars



@action
def run_job_from_alert(event: PrometheusKubernetesAlert, params: JobParams):
    """
    Create a kubernetes job with the specified parameters

    In addition, the job pod receives the following alert parameters as environment variables

    ALERT_NAME

    ALERT_STATUS

    ALERT_OBJ_KIND - oneof pod/deployment/node/job/daemonset or None in case it's unknown

    ALERT_OBJ_NAME

    ALERT_OBJ_NAMESPACE (If present)

    ALERT_OBJ_NODE (If present)

    ALERT_LABEL_{LABEL_NAME} for every label on the alert. For example a label named `foo` becomes `ALERT_LABEL_FOO`

    """
    logging.info(f"Running run_job_from alert action for alert {event.alert_name}")
    job_name = to_kubernetes_name(params.name)
    job: Job = Job(
        metadata=ObjectMeta(name=job_name, namespace=params.namespace),
        spec=JobSpec(
            template=PodTemplateSpec(
                spec=PodSpec(
                    containers=[
                        Container(
                            name=params.name,
                            image=params.image,
                            command=params.command,
                            env=__get_alert_env_vars(event, params),
                        )
                    ],
                    serviceAccountName=params.service_account,
                    restartPolicy=params.restart_policy,
                )
            ),
            backoffLimit=params.backoff_limit,
            activeDeadlineSeconds=params.active_deadline_seconds,
            ttlSecondsAfterFinished=params.job_ttl_after_finished,
        ),
    )

    job.create()

    if params.notify:
        event.add_enrichment([MarkdownBlock(f"**Action Job Information** \n Created Job from alert: *{job_name}*.")])

    if params.wait_for_completion:
        try:
            wait_until_job_complete(job, params.completion_timeout)
            job = hikaru.from_dict(job.to_dict(), cls=RobustaJob)  # temporary workaround for https://github.com/haxsaw/hikaru/issues/15
            pod = job.get_single_pod()
            event.add_enrichment([
                FileBlock("job-runner-logs.txt", pod.get_logs())
                ])
        except Exception as e:
            print(e, str(e))
            if str(e) != "Failed to reach wait condition":
                print("ERROR TRUE")
                logging.warning(f"Action Job stopped due to Exception {e}")
            else:
                err_str = f"Action Job {job_name} timed out. Could not fetch output"
                logging.warning(err_str)
                event.add_enrichment([MarkdownBlock(err_str)])

