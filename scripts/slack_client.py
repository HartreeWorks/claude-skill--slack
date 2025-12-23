#!/usr/bin/env python3
"""
Slack client using browser tokens (xoxc/xoxd) for stealth mode.
Supports multiple workspaces with contextual auto-selection.
"""

import requests
import json
import sys
from pathlib import Path
from datetime import datetime, timedelta

SKILL_ROOT = Path(__file__).parent.parent
CONFIG_PATH = SKILL_ROOT / "config.json"
SESSION_STATE_PATH = SKILL_ROOT / "session-state.json"


class SlackClient:
    BASE_URL = "https://slack.com/api"

    def __init__(self, xoxc_token: str, xoxd_token: str, user_agent: str = None):
        self.token = xoxc_token
        self.cookies = {"d": xoxd_token}
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/143.0.0.0 Safari/537.36"
        )
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Accept-Language": "en-NZ,en-AU;q=0.9,en;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
        })

    def _post(self, endpoint: str, data: dict = None) -> dict:
        """Make authenticated POST request with stealth fields."""
        payload = {
            "token": self.token,
            "_x_reason": "api-call",
            "_x_mode": "online",
            "_x_sonic": "true",
            "_x_app_name": "client",
            **(data or {})
        }

        response = self.session.post(
            f"{self.BASE_URL}/{endpoint}",
            data=payload,
            cookies=self.cookies
        )
        return response.json()

    # ==================== Core Functions ====================

    def channels_list(self, types: str = "public_channel,private_channel,im,mpim",
                      limit: int = 200) -> dict:
        """List channels, DMs, and group DMs."""
        return self._post("conversations.list", {
            "types": types,
            "limit": str(limit),
            "exclude_archived": "true"
        })

    def conversations_history(self, channel: str, limit: int = 100) -> dict:
        """Get message history from a channel or DM."""
        return self._post("conversations.history", {
            "channel": channel,
            "limit": str(limit)
        })

    def conversations_replies(self, channel: str, thread_ts: str) -> dict:
        """Get replies in a thread."""
        return self._post("conversations.replies", {
            "channel": channel,
            "ts": thread_ts
        })

    def search_messages(self, query: str, count: int = 20, sort: str = "timestamp") -> dict:
        """Search for messages. Supports Slack search modifiers like from:, in:, after:, etc."""
        return self._post("search.messages", {
            "query": query,
            "count": str(count),
            "sort": sort,
            "sort_dir": "desc"
        })

    def post_message(self, channel: str, text: str, thread_ts: str = None) -> dict:
        """Send a message to a channel, DM, or thread."""
        data = {
            "channel": channel,
            "text": text,
            "unfurl_links": "true",
            "unfurl_media": "true"
        }
        if thread_ts:
            data["thread_ts"] = thread_ts
        return self._post("chat.postMessage", data)

    def users_list(self, limit: int = 200) -> dict:
        """List all users in the workspace."""
        return self._post("users.list", {"limit": str(limit)})

    def auth_test(self) -> dict:
        """Test authentication and get current user info."""
        return self._post("auth.test")

    def get_permalink(self, channel: str, message_ts: str, workspace: str = None,
                      link_style: str = "app") -> str:
        """
        Generate a permalink for a message.

        Args:
            channel: Channel ID (e.g., C04AFNMCNFP)
            message_ts: Message timestamp (e.g., 1734567890.123456)
            workspace: Workspace name (e.g., "80000hours"). If not provided,
                      fetches from auth.test API.
            link_style: "app" for native Slack app, "browser" for web browser.
                       - app: uses /archives/ path (opens in Slack app)
                       - browser: uses /messages/ path (opens in browser)

        Returns:
            Permalink URL
        """
        if not workspace:
            auth = self.auth_test()
            if not auth.get("ok"):
                raise ValueError(f"Failed to get workspace: {auth.get('error')}")
            # Extract workspace from URL like "https://80000hours.slack.com/"
            url = auth.get("url", "")
            workspace = url.replace("https://", "").replace(".slack.com/", "")

        # Format timestamp: remove the "." to create the permalink format
        formatted_ts = message_ts.replace(".", "")

        # Choose path based on link style
        path = "messages" if link_style == "browser" else "archives"

        return f"https://{workspace}.slack.com/{path}/{channel}/p{formatted_ts}"


# ==================== Session State Management ====================

def load_session_state() -> dict:
    """Load current session state."""
    if SESSION_STATE_PATH.exists():
        with open(SESSION_STATE_PATH) as f:
            return json.load(f)
    return {
        "active_workspace": None,
        "last_action_timestamp": None,
        "workspace_channel_map": {}
    }


def save_session_state(state: dict):
    """Save session state."""
    with open(SESSION_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def set_active_workspace(workspace: str):
    """Set the active workspace for this session."""
    state = load_session_state()
    state["active_workspace"] = workspace
    state["last_action_timestamp"] = datetime.now().isoformat()
    save_session_state(state)


def get_active_workspace() -> str | None:
    """Get the active workspace if recent (within 10 minutes)."""
    state = load_session_state()
    active = state.get("active_workspace")
    last_action = state.get("last_action_timestamp")

    if active and last_action:
        try:
            last = datetime.fromisoformat(last_action)
            if datetime.now() - last < timedelta(minutes=10):
                return active
        except (ValueError, TypeError):
            pass
    return None


def record_channel_workspace(channel_id: str, workspace: str):
    """Record which workspace a channel belongs to."""
    state = load_session_state()
    state["workspace_channel_map"][channel_id] = workspace
    save_session_state(state)


def infer_workspace_from_channel(channel_id: str) -> str | None:
    """Try to infer workspace from a channel ID."""
    state = load_session_state()
    return state.get("workspace_channel_map", {}).get(channel_id)


# ==================== Cache Management ====================

def get_cache_path(workspace: str) -> Path:
    """Get the cache file path for a specific workspace."""
    return SKILL_ROOT / f"slack-cache-{workspace}.json"


def load_cache(workspace: str) -> dict:
    """Load cache for a specific workspace."""
    cache_path = get_cache_path(workspace)
    if cache_path.exists():
        with open(cache_path) as f:
            return json.load(f)
    return {
        "user": None,
        "self_dm_channel": None,
        "workspace": workspace,
        "frequent_contacts": {},
        "channels": {},
        "last_updated": None
    }


def save_cache(workspace: str, cache: dict):
    """Save cache for a specific workspace."""
    cache_path = get_cache_path(workspace)
    with open(cache_path, "w") as f:
        json.dump(cache, f, indent=2)


# ==================== Config Management ====================

def load_full_config() -> dict:
    """Load the full config file."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config not found: {CONFIG_PATH}")
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(config: dict):
    """Save the config file."""
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def load_config(workspace: str = None) -> tuple[dict, str]:
    """
    Load credentials for a workspace.

    Args:
        workspace: Explicit workspace name. If None, uses selection priority.

    Returns:
        tuple: (credentials_dict, workspace_name)
    """
    config = load_full_config()
    workspaces = config.get("workspaces", {})

    if not workspaces:
        raise ValueError("No workspaces configured in config.json")

    # 1. Explicit workspace requested
    if workspace:
        if workspace not in workspaces:
            raise ValueError(f"Unknown workspace: {workspace}. Available: {list(workspaces.keys())}")
        return workspaces[workspace], workspace

    # 2. Try active workspace from session state (if recent)
    active = get_active_workspace()
    if active and active in workspaces:
        return workspaces[active], active

    # 3. Fall back to default
    default = config.get("default_workspace")
    if default and default in workspaces:
        return workspaces[default], default

    # 4. Last resort: first workspace
    first_ws = next(iter(workspaces))
    return workspaces[first_ws], first_ws


def get_link_style() -> str:
    """Get the configured link style preference."""
    config = load_full_config()
    return config.get("link_style", "app")


# ==================== CLI ====================

def parse_global_args() -> tuple[str | None, list[str]]:
    """
    Parse global flags like --workspace/-w.

    Returns:
        tuple: (workspace_name or None, remaining args)
    """
    workspace = None
    args_filtered = []
    i = 1  # Skip script name

    while i < len(sys.argv):
        if sys.argv[i] in ("--workspace", "-w"):
            if i + 1 < len(sys.argv):
                workspace = sys.argv[i + 1]
                i += 2
                continue
            else:
                print(json.dumps({"error": "--workspace requires a value"}))
                sys.exit(1)
        args_filtered.append(sys.argv[i])
        i += 1

    return workspace, args_filtered


def main():
    """CLI interface for the Slack client."""
    workspace_arg, args = parse_global_args()

    if len(args) < 1:
        print(json.dumps({
            "error": "No command provided",
            "usage": "slack_client.py [-w workspace] <command> [args]",
            "commands": {
                "auth": "Test authentication",
                "channels": "List channels (optional: types)",
                "users": "List users",
                "history": "Get channel history (channel_id, optional: limit)",
                "replies": "Get thread replies (channel_id, thread_ts)",
                "search": "Search messages (query, optional: count)",
                "send": "Send message (channel_id, text, optional: thread_ts)",
                "permalink": "Get message permalink (channel_id, message_ts, optional: workspace, link_style)",
                "workspaces": "List configured workspaces",
                "switch": "Switch active workspace (workspace_name)",
                "add-workspace": "Add a new workspace (name, xoxc, xoxd, optional: user_agent)"
            }
        }, indent=2))
        sys.exit(1)

    command = args[0]
    cmd_args = args[1:]

    # Commands that don't need a client
    if command == "workspaces":
        config = load_full_config()
        state = load_session_state()
        result = {
            "workspaces": list(config.get("workspaces", {}).keys()),
            "default": config.get("default_workspace"),
            "active": state.get("active_workspace"),
            "link_style": config.get("link_style", "app")
        }
        print(json.dumps(result, indent=2))
        return

    if command == "switch":
        if not cmd_args:
            print(json.dumps({"error": "workspace name required"}))
            sys.exit(1)
        ws = cmd_args[0]
        config = load_full_config()
        if ws not in config.get("workspaces", {}):
            result = {"error": f"Unknown workspace: {ws}",
                      "available": list(config.get("workspaces", {}).keys())}
        else:
            set_active_workspace(ws)
            result = {"ok": True, "active_workspace": ws}
        print(json.dumps(result, indent=2))
        return

    if command == "add-workspace":
        if len(cmd_args) < 3:
            print(json.dumps({
                "error": "Usage: add-workspace <name> <xoxc_token> <xoxd_token> [user_agent]"
            }))
            sys.exit(1)
        name, xoxc, xoxd = cmd_args[0], cmd_args[1], cmd_args[2]
        user_agent = cmd_args[3] if len(cmd_args) > 3 else None

        config = load_full_config()
        if "workspaces" not in config:
            config["workspaces"] = {}

        # Use existing user_agent from another workspace if not provided
        if not user_agent and config["workspaces"]:
            first_ws = next(iter(config["workspaces"]))
            user_agent = config["workspaces"][first_ws].get("user_agent")

        config["workspaces"][name] = {
            "xoxc_token": xoxc,
            "xoxd_token": xoxd,
            "user_agent": user_agent
        }

        # Set as default if first workspace
        if not config.get("default_workspace"):
            config["default_workspace"] = name

        save_config(config)
        result = {"ok": True, "added": name, "workspaces": list(config["workspaces"].keys())}
        print(json.dumps(result, indent=2))
        return

    # Commands that need a client
    try:
        creds, ws_name = load_config(workspace_arg)
        client = SlackClient(
            creds["xoxc_token"],
            creds["xoxd_token"],
            creds.get("user_agent")
        )
    except Exception as e:
        print(json.dumps({"error": f"Failed to load config: {e}"}))
        sys.exit(1)

    try:
        if command == "auth":
            result = client.auth_test()
            if result.get("ok"):
                # Update session state on successful auth
                set_active_workspace(ws_name)
                result["_workspace"] = ws_name

        elif command == "channels":
            types = cmd_args[0] if cmd_args else "public_channel,private_channel,im,mpim"
            result = client.channels_list(types=types)
            if result.get("ok"):
                set_active_workspace(ws_name)
                result["_workspace"] = ws_name

        elif command == "users":
            result = client.users_list()
            if result.get("ok"):
                set_active_workspace(ws_name)
                result["_workspace"] = ws_name

        elif command == "history":
            if not cmd_args:
                print(json.dumps({"error": "channel_id required"}))
                sys.exit(1)
            channel = cmd_args[0]
            limit = int(cmd_args[1]) if len(cmd_args) > 1 else 100
            result = client.conversations_history(channel, limit)
            if result.get("ok"):
                set_active_workspace(ws_name)
                record_channel_workspace(channel, ws_name)
                result["_workspace"] = ws_name

        elif command == "replies":
            if len(cmd_args) < 2:
                print(json.dumps({"error": "channel_id and thread_ts required"}))
                sys.exit(1)
            channel = cmd_args[0]
            result = client.conversations_replies(channel, cmd_args[1])
            if result.get("ok"):
                set_active_workspace(ws_name)
                record_channel_workspace(channel, ws_name)
                result["_workspace"] = ws_name

        elif command == "search":
            if not cmd_args:
                print(json.dumps({"error": "query required"}))
                sys.exit(1)
            query = cmd_args[0]
            count = int(cmd_args[1]) if len(cmd_args) > 1 else 20
            result = client.search_messages(query, count)
            if result.get("ok"):
                set_active_workspace(ws_name)
                result["_workspace"] = ws_name

        elif command == "send":
            if len(cmd_args) < 2:
                print(json.dumps({"error": "channel_id and text required"}))
                sys.exit(1)
            channel = cmd_args[0]
            text = cmd_args[1]
            thread_ts = cmd_args[2] if len(cmd_args) > 2 else None
            result = client.post_message(channel, text, thread_ts)
            if result.get("ok"):
                set_active_workspace(ws_name)
                record_channel_workspace(channel, ws_name)
                result["_workspace"] = ws_name

        elif command == "permalink":
            if len(cmd_args) < 2:
                print(json.dumps({"error": "channel_id and message_ts required"}))
                sys.exit(1)
            channel = cmd_args[0]
            message_ts = cmd_args[1]
            workspace = cmd_args[2] if len(cmd_args) > 2 else ws_name
            link_style = cmd_args[3] if len(cmd_args) > 3 else get_link_style()
            permalink = client.get_permalink(channel, message_ts, workspace, link_style)
            result = {"ok": True, "permalink": permalink, "_workspace": ws_name}

        else:
            print(json.dumps({"error": f"Unknown command: {command}"}))
            sys.exit(1)

        print(json.dumps(result, indent=2))

    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
