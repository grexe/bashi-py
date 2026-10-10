import json
import subprocess
import os
import re
from flask import Flask, request, jsonify, abort
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# --- CONFIGURATION FROM .ENV ---
# The host/port BASHI listens on
BASHI_HOST = os.getenv("BASHI_HOST", "0.0.0.0")
BASHI_PORT = int(os.getenv("BASHI_PORT", 5001))

# ZFS settings
ZFS_POOL_NAME = os.getenv("ZFS_POOL_NAME")
ZFS_SERVER_MOUNT_ROOT = os.getenv("ZFS_SERVER_MOUNT_ROOT")

# Basic Configuration Validation
if not ZFS_POOL_NAME or not ZFS_SERVER_MOUNT_ROOT:
    raise EnvironmentError("Missing ZFS_POOL_NAME or ZFS_SERVER_MOUNT_ROOT in .env file.")

# --- FLASK APP INITIALIZATION ---
app = Flask(__name__)


# --- CORE UTILITIES ---

def run_cmd(cmd_list, suppress_errors=False):
    """Safely runs a shell command and returns stdout or raises HTTP error."""
    try:
        # Check=True raises CalledProcessError for non-zero exit codes
        # shell=True is needed for commands that rely on environment/pipes, but try to avoid it
        result = subprocess.run(cmd_list, capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.strip()
        app.logger.error(f"Command failed: {' '.join(e.cmd)} | Error: {error_msg}")

        if "No such file or directory" in error_msg:
            abort(404, description=f"File not found on server or invalid command path: {error_msg}")
        if "Permission denied" in error_msg or "Operation not permitted" in error_msg:
            abort(403, description=f"Permission denied for attribute operation: {error_msg}")

        if suppress_errors:
            return ""

        # Fallback for generic errors
        abort(500, description=f"Shell command error: {error_msg}")
    except FileNotFoundError:
        abort(501, description=f"Required command not found. Check Docker PATH and privileges.")


# --- ZFS ID LOGIC ---

def resolve_path_to_ids(full_path):
    """
    Resolves an absolute server path to its stable ZFS IDs (pool, objset, object).
    Requires ZFS privileges and relies on parsing complex ZFS CLI output.
    """
    if not full_path.startswith(ZFS_SERVER_MOUNT_ROOT):
        abort(400, description=f"Path must start with configured root: {ZFS_SERVER_MOUNT_ROOT}")

    if not os.path.exists(full_path):
        abort(404, description=f"File not found on server at {full_path}")

    # 1. Get the ZFS Dataset name (requires parsing mount hierarchy)
    # Simplified: Find the dataset name that contains the path.
    # We assume the dataset name is ZFS_POOL_NAME/dataset_name for the purpose of zfs get objsetid
    # We will use the common format: ZFS_POOL_NAME/dataset_name
    dataset_name = os.path.dirname(full_path).replace(ZFS_SERVER_MOUNT_ROOT, f"{ZFS_POOL_NAME}/dataset")

    # 2. Get the Object Set ID (objset-id)
    # Command: zfs get -H -o value objsetid <dataset>
    try:
        # NOTE: This command is often brittle. We rely on ZFS get returning the objsetid.
        objset_id = run_cmd(['zfs', 'get', '-H', '-o', 'value', 'objsetid', dataset_name])
    except Exception as e:
        app.logger.error(f"Failed to get objsetid for {dataset_name}: {e}")
        # Mocking the ZFS ID retrieval if command fails (FOR DEV/TEST ONLY)
        objset_id = "MOCK_OBJSET_12345"

        # 3. Get the Dnode ID (objectid) using zdb
    # Command: zdb -dddd <dataset> <full_path> (This requires parsing Dnode DId 0x...)
    # This is the most privileged and brittle part. We'll use the mocked logic for the POC.
    dnode_id_output = run_cmd(['zdb', '-dddd', dataset_name, full_path])

    match = re.search(r'Dnode DId\s+0x([0-9a-f]+)', dnode_id_output, re.IGNORECASE)
    if not match:
        # Fallback/Error state if zdb output structure changes
        app.logger.error(f"Could not parse Dnode ID from ZDB output for {full_path}.")
        # Use a consistent error state or default ID
        object_id = "MOCK_OBJECT_ID_0"
    else:
        # Convert hex output to decimal string
        object_id = str(int(match.group(1), 16))

    return {
        "pool": ZFS_POOL_NAME,
        "object_set_id": objset_id,
        "object_id": object_id
    }


def resolve_ids_to_path(pool_name, objset_id, object_id):
    """Reverse converts ZFS IDs to the current file path using zfs_ids_to_path."""
    cmd = ['zfs_ids_to_path', pool_name, objset_id, object_id]

    path_output = run_cmd(cmd)

    # The output is expected to be the full path on the last line.
    resolved_path = path_output.split('\n')[-1]

    if not os.path.exists(resolved_path):
        abort(404, description=f"ZFS IDs {objset_id}:{object_id} resolved to non-existent path: {resolved_path}")

    return resolved_path


# --- BASHI API ENDPOINTS ---

@app.route('/api/bashi/resolve', methods=['GET'])
def bashi_resolve():
    """GET /api/bashi/resolve?path=/abs/path/to/file (Path -> ZFS IDs)"""
    client_path = request.args.get('path')
    if not client_path:
        return jsonify({"error": "Missing 'path' parameter"}), 400

    full_path = os.path.normpath(client_path)
    ids = resolve_path_to_ids(full_path)

    return jsonify(ids), 200


@app.route('/api/bashi/read', methods=['GET'])
def bashi_read():
    """GET /api/bashi/read?object_set_id=...&object_id=...&pool=... (Read xattrs)"""
    objset_id = request.args.get('object_set_id')
    object_id = request.args.get('object_id')
    pool_name = request.args.get('pool')

    if not objset_id or not object_id or not pool_name:
        return jsonify({"error": "Missing object_set_id, object_id, or pool parameter"}), 400

    file_path = resolve_ids_to_path(pool_name, objset_id, object_id)

    # Read ALL user attributes
    attr_output = run_cmd(['getfattr', '-d', '-m', 'user.', file_path], suppress_errors=True)

    attributes = {}
    for line in attr_output.split('\n'):
        match = re.match(r'user\.([\w.-]+)=\"(.*)\"', line)
        if match:
            attr_name = match.group(1)
            attr_value = match.group(2).strip('"')

            # Attempt to deserialize JSON values
            try:
                attributes[attr_name] = json.loads(attr_value)
            except json.JSONDecodeError:
                attributes[attr_name] = attr_value

    return jsonify({"attributes": attributes, "path": file_path}), 200


@app.route('/api/bashi/write', methods=['POST'])
def bashi_write():
    """POST /api/bashi/write (Write xattrs using ZFS IDs)"""
    data = request.json
    objset_id = data.get('object_set_id')
    object_id = data.get('object_id')
    pool_name = data.get('pool')
    attributes = data.get('attributes')

    if not objset_id or not object_id or not pool_name or not attributes:
        return jsonify({"error": "Missing required fields in payload"}), 400

    file_path = resolve_ids_to_path(pool_name, objset_id, object_id)

    written_attrs = {}
    for name, value in attributes.items():
        attr_name = f"user.{name}"
        serialized_value = json.dumps(value)

        run_cmd(['setfattr', '-n', attr_name, '-v', serialized_value, file_path])
        written_attrs[name] = value

    return jsonify({"status": "OK", "written_attributes": written_attrs, "path": file_path}), 200


if __name__ == '__main__':
    print(f"BASHI Helper starting on {BASHI_HOST}:{BASHI_PORT}")
    app.run(host=BASHI_HOST, port=BASHI_PORT, debug=False)
