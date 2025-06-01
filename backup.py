import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

from logprise import logger
import requests

from api_request import request
from config import CLIENT_API_URL, POST_BACKUP_SCRIPT, ROTATE

CACHE_DIR = (
    Path.home() / ".cache" / "pterodactyl-automated-backups" / "server_last_backup"
)


def get_last_offline_backup_time(server_id: str) -> Optional[datetime]:
    """Get timestamp of last offline backup for server."""
    timestamp_file = CACHE_DIR / f"{server_id}.timestamp"

    if not timestamp_file.exists():
        return None

    try:
        timestamp_str = timestamp_file.read_text().strip()
        return datetime.fromisoformat(timestamp_str)
    except (OSError, ValueError) as e:
        logger.debug(f"[{server_id}] Failed to read timestamp file: {e}")
        return None


def set_last_offline_backup_time(server_id: str, timestamp: datetime) -> None:
    """Save timestamp of offline backup for server."""
    timestamp_file = CACHE_DIR / f"{server_id}.timestamp"

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        timestamp_file.write_text(timestamp.isoformat())
        logger.debug(
            f"[{server_id}] Saved offline backup timestamp: {timestamp.isoformat()}"
        )
    except OSError as e:
        logger.error(f"[{server_id}] Failed to save timestamp: {e}")


def clear_last_offline_backup_time(server_id: str) -> None:
    """Clear offline backup timestamp for server."""
    timestamp_file = CACHE_DIR / f"{server_id}.timestamp"

    try:
        timestamp_file.unlink(missing_ok=True)
        logger.debug(f"[{server_id}] Cleared offline backup timestamp")
    except OSError as e:
        logger.error(f"[{server_id}] Failed to clear timestamp: {e}")


def cleanup_orphaned_timestamps(active_server_ids: set[str]) -> None:
    """Remove timestamp files for servers that no longer exist."""
    if not CACHE_DIR.exists():
        return

    try:
        for timestamp_file in CACHE_DIR.glob("*.timestamp"):
            server_id = timestamp_file.stem
            if server_id not in active_server_ids:
                timestamp_file.unlink()
                logger.debug(f"Removed orphaned timestamp file for server: {server_id}")
    except OSError as e:
        logger.error(f"Failed to cleanup orphaned timestamps: {e}")


def is_server_online(server_id: str) -> bool:
    """Check if server is online."""
    try:
        url = f"{CLIENT_API_URL}/servers/{server_id}/resources"
        response = request(url)
        current_state = response["attributes"]["current_state"]

        logger.debug(f"[{server_id}] Server state: {current_state}")
        return current_state != "offline"

    except requests.exceptions.RequestException as e:
        logger.error(f"[{server_id}] Error checking server status: {e}")
        return True  # Assume online if can't check


def should_backup_offline_server(server_id: str) -> bool:
    """Check if offline server should be backed up."""
    last_backup = get_last_offline_backup_time(server_id)

    if last_backup is None:
        logger.info(f"[{server_id}] Server offline, no previous offline backup found")
        set_last_offline_backup_time(server_id, datetime.now())
        return True

    logger.info(
        f"[{server_id}] Server offline, already backed up at {last_backup.isoformat()}"
    )
    return False


def remove_old_backup(server: Dict[str, Any]) -> None:
    """Remove older backups when limit is reached."""
    server_id = server["attributes"]["identifier"]
    backup_limit = server["attributes"]["feature_limits"]["backups"]
    server_name = server["attributes"]["name"]

    logger.info(
        f"[{server_id}] Checking backups for '{server_name}' (limit: {backup_limit})"
    )

    if backup_limit == 0:
        logger.info(f"[{server_id}] No backups needed for this server")
        return

    try:
        url = f"{CLIENT_API_URL}/servers/{server_id}/backups"
        response = request(url)
        backups = sorted(response["data"], key=lambda b: b["attributes"]["created_at"])
        backup_count = len(backups)

        if backup_count < backup_limit:
            logger.info(
                f"[{server_id}] No backups need removal ({backup_count}/{backup_limit})"
            )
            return

        to_delete = backup_count - backup_limit + 1
        logger.info(f"[{server_id}] Need to remove {to_delete} backup(s)")

        deleted = 0
        for backup in backups:
            if deleted >= to_delete:
                break

            if backup["attributes"]["is_locked"]:
                logger.warning(
                    f"[{server_id}] Backup '{backup['attributes']['name']}' is locked, skipping"
                )
                continue

            backup_name = backup["attributes"]["name"]
            backup_uuid = backup["attributes"]["uuid"]

            url = f"{CLIENT_API_URL}/servers/{server_id}/backups/{backup_uuid}"
            logger.info(f"[{server_id}] Removing backup: '{backup_name}'")

            request(url, method="DELETE")
            deleted += 1
            time.sleep(2)

        remaining_to_delete = to_delete - deleted
        if remaining_to_delete > 0:
            logger.error(
                f"[{server_id}] Failed to delete {remaining_to_delete} backups (locked)"
            )

    except requests.exceptions.RequestException as e:
        logger.error(f"[{server_id}] Error deleting backups: {e}")


def create_backup(server_id: str) -> Optional[str]:
    """Create backup and return UUID."""
    url = f"{CLIENT_API_URL}/servers/{server_id}/backups"
    backup = request(url, method="POST", data={"per_page": 100})
    backup_uuid = backup["attributes"]["uuid"]

    logger.info(f"[{server_id}] Backup started (UUID: {backup_uuid})")
    return backup_uuid


def wait_for_backup_completion(server_id: str, backup_uuid: str) -> None:
    """Wait for backup to complete and run post-backup script."""
    logger.info(f"[{server_id}] Waiting for backup completion...")

    url = f"{CLIENT_API_URL}/servers/{server_id}/backups/{backup_uuid}"
    wait_start = time.time()
    completion_checks = 0

    while True:
        completion_checks += 1
        response = request(url, data={"per_page": 100})

        if response["attributes"]["completed_at"]:
            elapsed = time.time() - wait_start
            logger.info(f"[{server_id}] Backup completed after {elapsed:.1f}s")
            run_script(server_id, backup_uuid)
            break

        if completion_checks % 6 == 0:  # Log every minute
            elapsed = time.time() - wait_start
            logger.info(
                f"[{server_id}] Still waiting for completion ({elapsed:.1f}s elapsed)"
            )

        time.sleep(10)


def process_server_backup(server: Dict[str, Any]) -> bool:
    """Process backup for a single server. Returns True on success."""
    server_attr = server["attributes"]
    server_id = server_attr["identifier"]
    server_name = server_attr["name"]
    backup_limit = server_attr["feature_limits"]["backups"]

    logger.info(f"[{server_id}] Processing '{server_name}'")

    if backup_limit == 0:
        logger.info(f"[{server_id}] Backup limit is 0, skipping")
        return True

    is_online = is_server_online(server_id)

    if is_online:
        # Server is online - clear offline backup state and proceed
        clear_last_offline_backup_time(server_id)
    else:
        # Server is offline - check if we should backup
        if not should_backup_offline_server(server_id):
            return True

    if ROTATE:
        remove_old_backup(server)

    backup_uuid = create_backup(server_id)

    if POST_BACKUP_SCRIPT:
        wait_for_backup_completion(server_id, backup_uuid)

    logger.info(f"[{server_id}] Backup process complete")
    return True


def backup_servers(all_servers: Dict[str, Any]) -> List[str]:
    """Backup all servers and return list of failed server IDs."""
    servers = all_servers["data"]
    server_count = len(servers)
    failed_servers = []

    logger.info(f"Starting backup process for {server_count} servers")

    # Cleanup orphaned timestamp files
    active_server_ids = {s["attributes"]["identifier"] for s in servers}
    cleanup_orphaned_timestamps(active_server_ids)

    for i, server in enumerate(servers, 1):
        server_id = server["attributes"]["identifier"]
        logger.info(f"[{server_id}] ({i}/{server_count})")

        try:
            if not process_server_backup(server):
                failed_servers.append(server_id)
            time.sleep(2)

        except requests.exceptions.RequestException as e:
            logger.error(f"[{server_id}] Error during backup: {e}")
            failed_servers.append(server_id)
            time.sleep(30)

    if failed_servers:
        logger.error(
            f"Backup completed with {len(failed_servers)} failures: {', '.join(failed_servers)}"
        )
    else:
        logger.success(f"Backup completed successfully for all {server_count} servers")

    return failed_servers


def run_script(server_id: str, backup_uuid: str) -> None:
    """Run post-backup script."""
    script_cmd = f"sh {POST_BACKUP_SCRIPT} {server_id} {backup_uuid}"
    logger.info(f"[{server_id}] Executing: {script_cmd}")

    exit_status = os.system(script_cmd)

    if exit_status > 0:
        logger.error(
            f"[{server_id}] Post-backup script failed with exit code {exit_status}"
        )
    else:
        logger.success(f"[{server_id}] Post-backup script completed successfully")


if __name__ == "__main__":
    logger.info(
        f"Backup script started, ROTATE={ROTATE}, POST_BACKUP_SCRIPT={POST_BACKUP_SCRIPT}"
    )

    try:
        server_list = request(
            f"{CLIENT_API_URL}/servers", data={"per_page": 100, "type": "admin"}
        )
        server_count = len(server_list.get("data", []))
        logger.info(f"Retrieved {server_count} servers")

        failed = backup_servers(server_list)

        if failed:
            exit_code = 1
            logger.warning(f"Exiting with code {exit_code} due to failed backups")
            exit(exit_code)

    except Exception as e:
        logger.exception(f"Unhandled exception in backup script: {e}")
        exit(2)
