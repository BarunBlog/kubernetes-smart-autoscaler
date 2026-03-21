import boto3
import os
import logging
from typing import List, Dict, Optional, Any
from botocore.exceptions import ClientError
from kubernetes import client, config
from kubernetes.client.models import V1Node, V1PodList
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)

class SmartScaler:
    def __init__(self, kubeconfig_path: str):
        self.asg_client = boto3.client('autoscaling')
        self.asg_name = os.environ['ASG_NAME']

        self.min_nodes = int(os.environ.get('MIN_NODES', 2))
        self.max_nodes = int(os.environ.get('MAX_NODES', 5))

        # Load Kubernetes Config
        config.load_kube_config(config_file=kubeconfig_path)
        self.k8s_api = client.CoreV1Api()

        # Thresholds
        self.scale_up_cpu = 70.0
        self.scale_down_cpu = 30.0

    def get_current_capacity(self):
        """Fetches the current Desired Capacity from AWS ASG."""
        try:
            response = self.asg_client.describe_auto_scaling_groups(
                AutoScalingGroupNames=[self.asg_name]
            )

            if not response['AutoScalingGroups']:
                raise ValueError(f"ASG with name {self.asg_name} not found.")

            return response['AutoScalingGroups'][0]['DesiredCapacity']

        except ClientError as e:
            logger.error(f"Failed to describe ASG: {e}")
            raise

    def get_target_node_for_removal(self) -> Optional[Dict[str, Any]]:
        """Finds the worker node with the fewest pods (ignoring system pods)"""

        try:
            nodes: List[V1Node] = self.k8s_api.list_node().items()
            worker_nodes: List[V1Node] = [
                node for node in nodes if 'node-role.kubernetes.io/master' not in node.metadata.labels
            ]

            node_scores: List[Dict[str, Any]] = []
            for node in worker_nodes:
                pods: List[V1PodList] = self.k8s_api.list_pod_for_all_namespaces(
                    field_selector=f"spec.nodeName={node.metadata.name}"
                ).items

                node_scores.append({
                    "name": node.metadata.name,
                    "instance_id": node.spec.provider_id.split('/')[-1],
                    "pod_count": len(pods)
                })

            if not node_scores:
                logger.warning("Targeted node not found, ")
                return None

            return sorted(node_scores, key=lambda x: x['pod_count'])[0] if node_scores else None

        except ApiException as e:
            logger.error(f"Kubernetes API error while selecting target node: {e}")
            return None

    def make_decision(self, cpu_utilization: float, pending_pods_count: int) -> int:
        """
        Business logic for scaling decisions.
        Prioritizes Scale-Up for availability, Conservative Scale-Down for stability.
        """
        current = self.get_current_capacity()
        logger.debug(f"Current Desired Capacity: {current}")

        # Scale Up (High CPU or Pending Pods)
        if cpu_utilization > self.scale_up_cpu or pending_pods_count > 0:
            if current < self.max_nodes:
                target = current + 1
                logger.info(
                    f"Decision: SCALE_UP to {target}. Reason: CPU={cpu_utilization}%, Pending={pending_pods_count}")
                return current + 1
            else:
                logger.warning("Max node limit reached. Cannot scale up further.")

        # Scale Down (Low CPU or no Pending Pods)
        elif cpu_utilization < self.scale_down_cpu and pending_pods_count == 0:
            if current > self.min_nodes:
                target = current - 1
                logger.info(f"Decision: SCALE_DOWN to {target}. Reason: CPU={cpu_utilization}%")
                return target

        return current  # No change

    def apply_scaling(self, current_capacity: int, new_capacity: int):
        """Executes the scaling command in AWS."""
        if new_capacity > current_capacity:
            self._scale_up(new_capacity)
        elif new_capacity < current_capacity:
            self._scale_down_graceful()

    def _scale_up(self, target):
        logger.info(f"Applying Scaling UP to {target}")
        try:
            self.asg_client.set_desired_capacity(
                AutoScalingGroupName=self.asg_name,
                DesiredCapacity=target,
                HonorCooldown=True,
                # prevents the Lambda from adding another node 2 minutes later
                # before the first one has even finished booting.
            )
        except ClientError as e:
            logger.error(f"AWS API Error while scaling up: {e}")
            raise

    def _scale_down_graceful(self):
        target_node = self.get_target_node_for_removal()
        if not target_node:
            logger.warning("No worker nodes found to scale down.")
            return

        instance_id: str = target_node['instance_id']
        node_name: str = target_node['name']

        logger.info(f"Applying Scaling Down")

        try:
            # 1. Cordon. It tells Do not put any new Pods on this node to k3s
            logger.info(f"Cordoning node: {node_name}")
            self.k8s_api.patch_node(node_name, {"spec": {"unschedulable": True}})

            # 2. Terminate via ASG (Automatically drains and decrements)
            logger.info(f"Requesting ASG to terminate and decrement: {instance_id}")
            self.asg_client.terminate_instance_in_auto_scaling_group(
                InstanceId=instance_id,
                ShouldDecrementDesiredCapacity=True
            )
        except ApiException as e:
            logger.error(f"K8s Cordon failed for {node_name}: {e}")
        except ClientError as e:
            logger.error(f"AWS Termination failed for {instance_id}: {e}")

