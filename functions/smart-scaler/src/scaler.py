import boto3
import os
import logging
import time
from typing import List, Dict, Optional, Any
from botocore.exceptions import ClientError
from kubernetes import client, config
from kubernetes.client.models import V1Node
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

        # Eviction

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
            nodes: List[V1Node] = self.k8s_api.list_node().items
            worker_nodes: List[V1Node] = [
                node for node in nodes
                if 'node-role.kubernetes.io/master' not in node.metadata.labels
                and 'node-role.kubernetes.io/control-plane' not in node.metadata.labels
            ]

            if not worker_nodes:
                logger.warning("No worker node found.")
                return None

            # get pods for all namespaces to avoid N+1 API calls
            all_pods = self.k8s_api.list_pod_for_all_namespaces().items

            # Initializing pod counter for each node to 0
            node_pod_counts = {node.metadata.name: 0 for node in worker_nodes}

            for pod in all_pods:
                node_name = pod.spec.node_name
                if node_name in node_pod_counts:

                    # ignore system namespaces
                    if pod.metadata.namespace in ["kube-system", "monitoring"]:
                        continue

                    # Ignore DaemonSets (they can't be 'drained')
                    is_daemonset = any(owner.kind == "DaemonSet" for owner in (pod.metadata.owner_references or []))
                    if is_daemonset:
                        continue

                    # Check if it's a RabbitMQ / Stateful pod
                    is_stateful = any(o.kind == "StatefulSet" for o in (pod.metadata.owner_references or []))

                    if is_stateful:
                        # Adding a 'weight' of 100 makes this node very unlikely to be chosen
                        # unless it is truly the last option.
                        node_pod_counts[node_name] += 100
                    else:
                        node_pod_counts[node_name] += 1

            node_scores: List[Dict[str, Any]] = []
            for node in worker_nodes:
                # Ensure provider_id exists before splitting
                if not node.spec.provider_id:
                    continue

                node_scores.append({
                    "name": node.metadata.name,
                    "instance_id": node.spec.provider_id.split('/')[-1],
                    "pod_count": node_pod_counts[node.metadata.name]
                })

            if not node_scores:
                logger.warning("Targeted node not found, ")
                return None

            return sorted(node_scores, key=lambda x: x['pod_count'])[0]

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

        node_name: str = target_node['name']

        # Convert Node Name to Real AWS Instance ID
        try:
            ec2 = boto3.client('ec2')
            # We filter by Private DNS Name because K3s names nodes after their DNS
            dns_query = ec2.describe_instances(
                Filters=[{'Name': 'private-dns-name', 'Values': [f"{node_name}.ap-southeast-1.compute.internal"]}]
            )

            # If the above fails, try filtering by Private IP (more robust)
            if not dns_query['Reservations']:
                ip_addr = node_name.replace('ip-', '').replace('-', '.')
                dns_query = ec2.describe_instances(
                    Filters=[{'Name': 'private-ip-address', 'Values': [ip_addr]}]
                )

            instance_id = dns_query['Reservations'][0]['Instances'][0]['InstanceId']
            logger.info(f"Mapped node {node_name} to AWS Instance ID: {instance_id}")

        except Exception as e:
            logger.error(f"Failed to resolve AWS Instance ID for {node_name}: {e}")
            return
        # -----------------------------------------------------------

        logger.info(f"Applying Scaling Down")

        try:
            # Cordon. It tells Do not put any new Pods on this node to k3s
            logger.info(f"Cordoning node: {node_name}")
            self.k8s_api.patch_node(node_name, {"spec": {"unschedulable": True}})

            # Eviction (The "Drain" part)
            wait_time = 30  # 30 sec
            pods = self.k8s_api.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={node_name}").items
            for pod in pods:
                # Skip DaemonSets and monitoring and infra pods (they can't be evicted)
                # If we try to delete them then the DaemonSets will automatically try to create them.
                # Causes race condition.
                is_ds = any(o.kind == "DaemonSet" for o in (pod.metadata.owner_references or []))
                if pod.metadata.namespace in ["kube-system", "monitoring"] or is_ds:
                    continue

                # SPECIAL HANDLING: RabbitMQ / StatefulSets
                # These need a longer grace period to hand over leadership
                is_stateful = any(o.kind == "StatefulSet" for o in (pod.metadata.owner_references or []))

                # If we find a StatefulSet, we need a much longer grace period for data sync
                if is_stateful:
                    wait_time = 120 # Increase to 2 minutes for RabbitMQ safety

                try:
                    eviction = client.V1Eviction(
                        metadata=client.V1ObjectMeta(name=pod.metadata.name, namespace=pod.metadata.namespace)
                    )
                    self.k8s_api.create_namespaced_pod_eviction(pod.metadata.name, pod.metadata.namespace, eviction)
                except ApiException as e:
                    if e.status != 404: raise  # Ignore if pod is already gone

            # Wait for pods to clear (Optional but recommended)
            logger.info(f"Waiting {wait_time}s for pod migration (Stateful={wait_time > 30})...")
            time.sleep(wait_time)

            # Terminate via ASG (Automatically drains and decrements)
            logger.info(f"Requesting ASG to terminate and decrement: {instance_id}")
            self.asg_client.terminate_instance_in_auto_scaling_group(
                InstanceId=instance_id,
                ShouldDecrementDesiredCapacity=True
            )
        except ApiException as e:
            logger.error(f"K8s Cordon failed for {node_name}: {e}")
        except ClientError as e:
            logger.error(f"AWS Termination failed for {instance_id}: {e}")

