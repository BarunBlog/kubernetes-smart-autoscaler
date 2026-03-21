import os
import time
import boto3
import logging

logger = logging.getLogger(__name__)

class ClusterConfigManager:
    KUBECONFIG_PATH = "/tmp/kubeconfig"

    def __init__(self):
        self.s3 = boto3.client('s3')
        self.bucket = os.environ.get('S3_BUCKET_NAME')

    def prepare_config(self) -> str:
        """Ensures a valid, fresh kubeconfig exists in /tmp."""
        if not self.bucket:
            raise ValueError("S3_BUCKET_NAME environment variable is missing.")

        if self._is_config_fresh():
            logger.debug("Using cached kubeconfig from /tmp")
            return self.KUBECONFIG_PATH

        return self._download_from_s3()

    def _is_config_fresh(self) -> bool:
        """Checks if file exists and is less than 1 hour old"""
        if not os.path.exists(self.KUBECONFIG_PATH):
            return False

        file_age = time.time() - os.path.getmtime(self.KUBECONFIG_PATH)
        return file_age < 3600 # 1 hour in seconds

    def _download_from_s3(self) -> str:
        """Downloads the prepared kubeconfig from S3."""
        try:
            logger.info(f"Downloading kubeconfig from s3://{self.bucket}/kubeconfig")
            self.s3.download_file(self.bucket, 'kubeconfig', self.KUBECONFIG_PATH)
            return self.KUBECONFIG_PATH
        except Exception as e:
            logger.error(f"Failed to download kubeconfig: {e}")
            raise
