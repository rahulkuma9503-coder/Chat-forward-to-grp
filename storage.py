# storage.py
import json
import os

STORAGE_FILE = "connections.json"

def load_connections():
    if not os.path.exists(STORAGE_FILE):
        return {}
    try:
        with open(STORAGE_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return {}

def save_connection(user_id, group_id):
    connections = load_connections()
    connections[str(user_id)] = group_id
    with open(STORAGE_FILE, 'w') as f:
        json.dump(connections, f)

def get_connection(user_id):
    connections = load_connections()
    return connections.get(str(user_id))
