import os
import logging
from scaler import SmartScaler
from cluster_config import ClusterConfigManager
from state_manager import StateManager
from metrics import PrometheusClient
from typing import Any, Dict

# Configuring the structured logging
logger = logging.getLogger()
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

DYNAMO_TABLE = os.environ.get('DYNAMO_TABLE')
state_manager = StateManager(DYNAMO_TABLE) if DYNAMO_TABLE else None
config_manager = ClusterConfigManager()

def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    AWS Lambda handler to orchestrate K3s cluster auto-scaling.
    """

    logger.info("Auto-scaling check initiated.", extra={"event": event})

    if not state_manager:
        logger.error("Environment variable DYNAMO_TABLE is not set.")
        return {"status": "error", "message": "Configuration error"}

    # Use Context Manager or Try/Finally for Lock Safety
    if not state_manager.acquire_lock():
        logger.warning("Scaling operation already in progress. Skipping execution.")
        return {"status": "skipped", "message": "Lock active"}

    try:
        # get kubeconfig path
        kubeconfig_path = config_manager.prepare_config()

        # Initialize the clients
        metrics_client = PrometheusClient()

        # Checking if the prometheus is online
        if not metrics_client.is_ready():
            logger.warning("Prometheus is not reachable. Cluster might be bootstrapping. Skipping check.")
            return {"status": "skipped", "message": "Prometheus offline"}

        scaler = SmartScaler(kubeconfig_path, state_manager)

        # Fetching Metrics
        cpu_usage = metrics_client.get_avg_cpu()
        pending_pods = metrics_client.get_pending_pods()

        logger.info(
            "Cluster Metrics Fetched",
            extra={"cpu": cpu_usage, "pending_pods": pending_pods}
        )

        # Scaling Logic
        current_capacity = scaler.get_current_capacity()
        recommended_capacity = scaler.make_decision(cpu_usage, pending_pods)

        if recommended_capacity != current_capacity:
            logger.info(
                "Capacity mismatch detected. Scaling...",
                extra={"from": current_capacity, "to": recommended_capacity}
            )
            scaler.apply_scaling(current_capacity, recommended_capacity)
        else:
            logger.info("Cluster capacity is optimal. No action taken.")

        return {"status": "success", "recommended_capacity": recommended_capacity}

    except Exception as e:
        logger.error(f"Scaling aborted due to safety failure: {e}")
        return {"status": "error", "message": "Scaling aborted for safety."}

    finally:
        state_manager.release_lock()
        logger.debug("State lock released.")