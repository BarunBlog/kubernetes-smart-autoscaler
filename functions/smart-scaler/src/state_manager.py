import boto3
from botocore.exceptions import ClientError
import time
import logging

logger = logging.getLogger(__name__)

class StateManager:
    def __init__(self, table_name: str):
        self.dynamodb = boto3.resource('dynamodb')
        self.table = self.dynamodb.Table(table_name)
        self.lock_id = "cluster_scaling_lock"

        # How long a lock is valid before it's considered "stale" (5 minutes)
        self.lock_duration = 300

    def acquire_lock(self) -> bool:
        """
        Attempts to acquire a distributed lock using DynamoDB Conditional Writes.
        Includes a safety check for stale locks.
        """
        now = int(time.time())
        expiration_time = now + self.lock_duration

        try:
            # Atomic operation: Put the item ONLY if:
            # The lock doesn't exist yet
            # The existing lock is marked as False (not locked)
            # The existing lock has expired (stale lock recovery)
            self.table.put_item(
                Item={
                    'LockID': self.lock_id,
                    'is_locked': True,
                    'last_updated': now,
                    'ttl': expiration_time  # DynamoDB can auto-delete this
                },
                ConditionExpression=(
                    "attribute_not_exists(LockID) OR "
                    "is_locked = :false_val OR "
                    "last_updated < :stale_time"
                ),
                ExpressionAttributeValues={
                    ":false_val": False,
                    ":stale_time": now - self.lock_duration
                }
            )
            logger.info("Scaling lock acquired successfully.")
            return True
        except ClientError as e:
            # Error code 'ConditionalCheckFailedException' means someone else has the lock
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                logger.warning("Failed to acquire lock: Scaling already in progress.")
            else:
                logger.error(f"DynamoDB error during lock acquisition: {e}")
            return False

    def release_lock(self):
        """
        Releases the lock by setting is_locked to False.
        """
        try:
            self.table.update_item(
                Key={'LockID': self.lock_id},
                UpdateExpression="SET is_locked = :f, last_updated = :t",
                ExpressionAttributeValues={
                    ":f": False,
                    ":t": int(time.time())
                }
            )
            logger.info("Scaling lock released.")
        except ClientError as e:
            logger.error(f"Failed to release lock: {e}")

    def get_last_scaling_time(self) -> int:
        """Fetch the timestamp of the last successful scaling action."""
        try:
            # Fetch only the 'timestamp' attribute to save bandwidth/RCUs
            response = self.table.get_item(
                Key={'LockID': 'last_scaling_event'},
                ProjectionExpression="#ts",
                ExpressionAttributeNames={"#ts": "timestamp"}
            )

            item = response.get('Item')
            if not item:
                logger.info("No previous scaling event found in DynamoDB. Starting fresh.")
                return 0

            return int(item.get('timestamp', 0))
        except ClientError as e:
            logger.error(f"DynamoDB ClientError fetching scaling time: {e.response['Error']['Message']}")
            return 0

    def update_scaling_timestamp(self):
        """Record that a scaling action just happened right now."""
        now = int(time.time())
        logger.info(f"[DynamoDB] Received command to save the scaling timestamp to {now}")

        try:
            self.table.put_item(
                Item={
                    'LockID': 'last_scaling_event',
                    'timestamp': int(time.time())
                }
            )
            logger.info(f"Successfully updated last scaling timestamp to {now}")
        except ClientError as e:
            logger.error(f"Failed to record scaling event in DynamoDB: {e.response['Error']['Message']}")
        except Exception as e:
            logger.critical(f"Unexpected system failure while updating DynamoDB: {e}", exc_info=True)
