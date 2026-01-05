#!/usr/bin/env python3
"""
Slack client using browser tokens (xoxc/xoxd) for stealth mode.
Supports multiple workspaces with contextual auto-selection.
"""

import requests
import json
import sys
import time
import uuid
import re
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

SKILL_ROOT = Path(__file__).parent.parent
CONFIG_PATH = SKILL_ROOT / "config.json"
SESSION_STATE_PATH = SKILL_ROOT / "session-state.json"


# ==================== Rate Limiter ====================

class RateLimiter:
    """
    Manages API call pacing for Slack's tiered rate limits.

    Tier 3 (search.messages): ~50 req/min
    Tier 4 (conversations.replies): ~100 req/min

    Uses conservative limits to avoid ever hitting actual rate limits.
    """

    TIER_3_LIMIT = 35  # search - conservative to never hit 50
    TIER_4_LIMIT = 70  # replies - conservative to never hit 100

    def __init__(self):
        self.tier3_calls = []  # timestamps of search calls
        self.tier4_calls = []  # timestamps of replies calls
        self.backoff_until = None
        self.consecutive_429s = 0

    def _prune_old_calls(self, calls: list, window_seconds: int = 60) -> list:
        """Remove calls older than the window."""
        cutoff = datetime.now() - timedelta(seconds=window_seconds)
        return [t for t in calls if t > cutoff]

    def _handle_backoff(self):
        """Sleep if we're in a backoff period."""
        if self.backoff_until and datetime.now() < self.backoff_until:
            sleep_time = (self.backoff_until - datetime.now()).total_seconds()
            if sleep_time > 0:
                print(f"  Rate limit backoff: sleeping {sleep_time:.1f}s", file=sys.stderr)
                time.sleep(sleep_time)
            self.backoff_until = None

    def wait_for_tier3(self):
        """Wait if needed before making a Tier 3 call (search)."""
        self._handle_backoff()
        self.tier3_calls = self._prune_old_calls(self.tier3_calls)

        if len(self.tier3_calls) >= self.TIER_3_LIMIT:
            oldest = min(self.tier3_calls)
            sleep_time = 60 - (datetime.now() - oldest).total_seconds() + 1
            if sleep_time > 0:
                print(f"  Tier 3 limit: sleeping {sleep_time:.1f}s", file=sys.stderr)
                time.sleep(sleep_time)
            self.tier3_calls = self._prune_old_calls(self.tier3_calls)

        self.tier3_calls.append(datetime.now())

    def wait_for_tier4(self):
        """Wait if needed before making a Tier 4 call (replies)."""
        self._handle_backoff()
        self.tier4_calls = self._prune_old_calls(self.tier4_calls)

        if len(self.tier4_calls) >= self.TIER_4_LIMIT:
            oldest = min(self.tier4_calls)
            sleep_time = 60 - (datetime.now() - oldest).total_seconds() + 1
            if sleep_time > 0:
                print(f"  Tier 4 limit: sleeping {sleep_time:.1f}s", file=sys.stderr)
                time.sleep(sleep_time)
            self.tier4_calls = self._prune_old_calls(self.tier4_calls)

        self.tier4_calls.append(datetime.now())

    def handle_rate_limit_response(self, retry_after: int = None):
        """Called when we receive a 429 or rate_limited error."""
        self.consecutive_429s += 1

        # Exponential backoff: 30s, 60s, 120s, 240s, max 5min
        if retry_after:
            wait_seconds = retry_after
        else:
            wait_seconds = min(30 * (2 ** (self.consecutive_429s - 1)), 300)

        self.backoff_until = datetime.now() + timedelta(seconds=wait_seconds)
        print(f"  Rate limited! Backing off {wait_seconds}s", file=sys.stderr)

    def reset_backoff(self):
        """Called after a successful request."""
        self.consecutive_429s = 0


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

    def search_messages_paginated(self, query: str, page: int = 1, count: int = 100,
                                   sort: str = "timestamp") -> dict:
        """
        Search messages with pagination support.

        Args:
            query: Slack search query (e.g., "from:@username after:2025-01-01")
            page: Page number (1-indexed)
            count: Results per page (max 100)
            sort: Sort order ('timestamp' or 'score')

        Returns:
            API response with 'messages.matches', 'messages.paging', etc.
        """
        return self._post("search.messages", {
            "query": query,
            "count": str(min(count, 100)),
            "page": str(page),
            "sort": sort,
            "sort_dir": "asc"  # Oldest first for consistent pagination
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


# ==================== Export State Management ====================

def get_export_state_path(workspace: str) -> Path:
    """Get the export state file path for a specific workspace."""
    return SKILL_ROOT / f"export-state-{workspace}.json"


def load_export_state(workspace: str) -> dict:
    """Load export state for resume capability."""
    state_path = get_export_state_path(workspace)
    if state_path.exists():
        with open(state_path) as f:
            return json.load(f)
    return None


def save_export_state(workspace: str, state: dict):
    """Save export state for resume capability."""
    state_path = get_export_state_path(workspace)
    state["updated_at"] = datetime.now().isoformat()
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)


def create_export_state(workspace: str, user_id: str, username: str,
                        from_date: str, to_date: str, output_file: str) -> dict:
    """Create a new export state."""
    return {
        "export_id": str(uuid.uuid4())[:8],
        "workspace": workspace,
        "status": "searching",
        "started_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "config": {
            "from_date": from_date,
            "to_date": to_date,
            "output_file": output_file,
            "user_id": user_id,
            "username": username
        },
        "search_progress": {
            "total_matches": 0,
            "current_page": 1,
            "messages_fetched": 0
        },
        "thread_progress": {
            "threads_pending": [],
            "threads_fetched": [],
            "current_index": 0
        },
        "data": {
            "channels": {},
            "threads": [],
            "standalone_messages": []
        },
        "errors": [],
        "stats": {
            "api_calls": 0
        }
    }


def delete_export_state(workspace: str):
    """Delete export state file after successful completion."""
    state_path = get_export_state_path(workspace)
    if state_path.exists():
        state_path.unlink()


# ==================== Export Functions ====================

def run_export(client: 'SlackClient', workspace: str, from_date: str, to_date: str,
               output_file: str, resume: bool = False):
    """
    Export user's messages with full thread context.

    Phase 1: Search for all user messages in date range
    Phase 2: Fetch full thread context for threaded messages
    Phase 3: Write final output file
    """
    rate_limiter = RateLimiter()

    # Get user info
    auth = client.auth_test()
    if not auth.get("ok"):
        raise Exception(f"Auth failed: {auth.get('error')}")

    user_id = auth.get("user_id")
    username = auth.get("user")

    # Load or create state
    state = load_export_state(workspace) if resume else None

    if state and state.get("status") == "completed":
        print(f"Previous export completed. Use without --resume to start fresh.", file=sys.stderr)
        return state

    if not state or not resume:
        state = create_export_state(workspace, user_id, username, from_date, to_date, output_file)
        save_export_state(workspace, state)
        print(f"Starting export {state['export_id']} for @{username}", file=sys.stderr)
    else:
        print(f"Resuming export {state['export_id']} from {state['status']}", file=sys.stderr)

    try:
        # ===== PHASE 1: Search for user's messages =====
        if state["status"] == "searching":
            query = f"from:@{username} after:{from_date} before:{to_date}"
            page = state["search_progress"]["current_page"]

            print(f"Phase 1: Searching for messages...", file=sys.stderr)

            while True:
                rate_limiter.wait_for_tier3()
                state["stats"]["api_calls"] += 1

                result = client.search_messages_paginated(query, page=page, count=100)

                if not result.get("ok"):
                    error = result.get("error", "unknown")
                    if error == "ratelimited":
                        rate_limiter.handle_rate_limit_response()
                        continue
                    raise Exception(f"Search failed: {error}")

                rate_limiter.reset_backoff()

                messages_data = result.get("messages", {})
                matches = messages_data.get("matches", [])
                total = messages_data.get("total", 0)
                paging = messages_data.get("paging", {})

                # Process messages from this page
                for msg in matches:
                    _process_search_result(msg, state)

                state["search_progress"]["total_matches"] = total
                state["search_progress"]["messages_fetched"] += len(matches)
                state["search_progress"]["current_page"] = page

                print(f"  Page {page}/{paging.get('pages', 1)}: "
                      f"{state['search_progress']['messages_fetched']}/{total} messages",
                      file=sys.stderr)

                save_export_state(workspace, state)

                # Check if done with search
                if page >= paging.get("pages", 1):
                    break

                page += 1

            state["status"] = "fetching_threads"
            save_export_state(workspace, state)

        # ===== PHASE 2: Fetch thread context =====
        if state["status"] == "fetching_threads":
            pending = state["thread_progress"]["threads_pending"]
            fetched = set(state["thread_progress"]["threads_fetched"])
            to_fetch = [t for t in pending if t not in fetched]

            print(f"Phase 2: Fetching {len(to_fetch)} threads...", file=sys.stderr)

            for i, thread_key in enumerate(to_fetch):
                channel_id, thread_ts = thread_key.split(":")

                rate_limiter.wait_for_tier4()
                state["stats"]["api_calls"] += 1

                result = client.conversations_replies(channel_id, thread_ts)

                if not result.get("ok"):
                    error = result.get("error", "unknown")
                    if error == "ratelimited":
                        rate_limiter.handle_rate_limit_response()
                        continue
                    elif error in ("thread_not_found", "channel_not_found", "not_in_channel"):
                        # Skip inaccessible threads
                        state["thread_progress"]["threads_fetched"].append(thread_key)
                        state["errors"].append({
                            "timestamp": datetime.now().isoformat(),
                            "type": error,
                            "thread": thread_key
                        })
                        continue
                    raise Exception(f"Thread fetch failed: {error}")

                rate_limiter.reset_backoff()

                # Store thread data
                thread_messages = result.get("messages", [])
                _store_thread_data(thread_key, thread_messages, state)
                state["thread_progress"]["threads_fetched"].append(thread_key)
                state["thread_progress"]["current_index"] = i + 1

                if (i + 1) % 10 == 0:
                    print(f"  Threads: {i + 1}/{len(to_fetch)}", file=sys.stderr)
                    save_export_state(workspace, state)

            state["status"] = "writing_output"
            save_export_state(workspace, state)

        # ===== PHASE 3: Write output file =====
        if state["status"] == "writing_output":
            print(f"Phase 3: Writing output...", file=sys.stderr)
            _write_export_file(state, workspace)

            state["status"] = "completed"
            state["completed_at"] = datetime.now().isoformat()
            save_export_state(workspace, state)

            print(f"\nExport complete!", file=sys.stderr)
            print(f"  Messages: {state['search_progress']['messages_fetched']}", file=sys.stderr)
            print(f"  Threads: {len(state['data']['threads'])}", file=sys.stderr)
            print(f"  Standalone: {len(state['data']['standalone_messages'])}", file=sys.stderr)
            print(f"  Output: {state['config']['output_file']}", file=sys.stderr)

        return state

    except KeyboardInterrupt:
        print(f"\nExport paused. Run with --resume to continue.", file=sys.stderr)
        state["status"] = "paused"
        save_export_state(workspace, state)
        raise
    except Exception as e:
        state["errors"].append({
            "timestamp": datetime.now().isoformat(),
            "type": type(e).__name__,
            "details": str(e)
        })
        save_export_state(workspace, state)
        raise


def _process_search_result(msg: dict, state: dict):
    """Process a message from search results."""
    channel_info = msg.get("channel", {})
    channel_id = channel_info.get("id")
    message_ts = msg.get("ts")
    thread_ts = msg.get("thread_ts")
    user_id = state["config"]["user_id"]

    # Check permalink for thread_ts if not in message directly
    # Permalink format: .../p1234567890123456?thread_ts=1234567890.123456
    if not thread_ts:
        permalink = msg.get("permalink", "")
        if "thread_ts=" in permalink:
            match = re.search(r'thread_ts=(\d+\.\d+)', permalink)
            if match:
                thread_ts = match.group(1)

    # Store channel metadata
    if channel_id and channel_id not in state["data"]["channels"]:
        state["data"]["channels"][channel_id] = {
            "id": channel_id,
            "name": channel_info.get("name", "unknown"),
            "type": _infer_channel_type(channel_id)
        }

    if thread_ts:
        # Message is part of a thread
        thread_key = f"{channel_id}:{thread_ts}"
        if thread_key not in state["thread_progress"]["threads_pending"]:
            state["thread_progress"]["threads_pending"].append(thread_key)
    else:
        # Standalone message
        state["data"]["standalone_messages"].append({
            "ts": message_ts,
            "channel_id": channel_id,
            "user": msg.get("user") or msg.get("username"),
            "text": msg.get("text", ""),
            "is_user_message": True,
            "permalink": msg.get("permalink")
        })


def _store_thread_data(thread_key: str, messages: list, state: dict):
    """Store thread messages."""
    channel_id, thread_ts = thread_key.split(":")
    user_id = state["config"]["user_id"]

    thread_data = {
        "thread_id": thread_key,
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "user_message_count": 0,
        "total_message_count": len(messages),
        "messages": []
    }

    for msg in messages:
        is_user = msg.get("user") == user_id
        if is_user:
            thread_data["user_message_count"] += 1

        thread_data["messages"].append({
            "ts": msg.get("ts"),
            "user": msg.get("user"),
            "text": msg.get("text", ""),
            "is_user_message": is_user
        })

    state["data"]["threads"].append(thread_data)


def _infer_channel_type(channel_id: str) -> str:
    """Infer channel type from ID prefix."""
    if channel_id.startswith("C"):
        return "channel"
    elif channel_id.startswith("D"):
        return "dm"
    elif channel_id.startswith("G"):
        return "group"
    return "unknown"


def _write_export_file(state: dict, workspace: str):
    """Write the final export JSON file."""
    output = {
        "metadata": {
            "export_id": state["export_id"],
            "workspace": workspace,
            "user": {
                "id": state["config"]["user_id"],
                "username": state["config"]["username"]
            },
            "date_range": {
                "from": state["config"]["from_date"],
                "to": state["config"]["to_date"]
            },
            "exported_at": datetime.now().isoformat(),
            "stats": {
                "total_messages": state["search_progress"]["messages_fetched"],
                "total_threads": len(state["data"]["threads"]),
                "standalone_messages": len(state["data"]["standalone_messages"]),
                "channels_count": len(state["data"]["channels"]),
                "api_calls": state["stats"]["api_calls"]
            }
        },
        "channels": state["data"]["channels"],
        "threads": state["data"]["threads"],
        "standalone_messages": state["data"]["standalone_messages"]
    }

    output_path = Path(state["config"]["output_file"]).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)


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
                "add-workspace": "Add a new workspace (name, xoxc, xoxd, optional: user_agent)",
                "export": "Export messages (--from DATE --to DATE --output FILE [--resume])",
                "export-status": "Check export status"
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

    if command == "export-status":
        # Check status without requiring workspace_arg - look at default/active
        try:
            _, ws_name = load_config(workspace_arg)
        except Exception:
            print(json.dumps({"error": "No workspace configured"}))
            sys.exit(1)

        state = load_export_state(ws_name)
        if not state:
            print(json.dumps({
                "ok": True,
                "workspace": ws_name,
                "status": "no_export",
                "message": "No export in progress or completed"
            }, indent=2))
        else:
            result = {
                "ok": True,
                "workspace": ws_name,
                "export_id": state.get("export_id"),
                "status": state.get("status"),
                "started_at": state.get("started_at"),
                "updated_at": state.get("updated_at"),
                "search_progress": state.get("search_progress"),
                "thread_progress": {
                    "pending": len(state.get("thread_progress", {}).get("threads_pending", [])),
                    "fetched": len(state.get("thread_progress", {}).get("threads_fetched", []))
                },
                "errors": len(state.get("errors", []))
            }
            if state.get("status") == "completed":
                result["output_file"] = state.get("config", {}).get("output_file")
            print(json.dumps(result, indent=2))
        return

    if command == "export":
        # Parse export-specific arguments
        from_date = None
        to_date = None
        output_file = None
        resume = False

        i = 0
        while i < len(cmd_args):
            if cmd_args[i] == "--from" and i + 1 < len(cmd_args):
                from_date = cmd_args[i + 1]
                i += 2
            elif cmd_args[i] == "--to" and i + 1 < len(cmd_args):
                to_date = cmd_args[i + 1]
                i += 2
            elif cmd_args[i] == "--output" and i + 1 < len(cmd_args):
                output_file = cmd_args[i + 1]
                i += 2
            elif cmd_args[i] == "--resume":
                resume = True
                i += 1
            else:
                i += 1

        # Validate args
        if not resume and (not from_date or not to_date or not output_file):
            print(json.dumps({
                "error": "Required: --from DATE --to DATE --output FILE (or --resume)",
                "usage": "export --from 2025-07-01 --to 2026-01-05 --output ~/slack-export.json",
                "resume_usage": "export --resume"
            }))
            sys.exit(1)

        try:
            creds, ws_name = load_config(workspace_arg)
            client = SlackClient(
                creds["xoxc_token"],
                creds["xoxd_token"],
                creds.get("user_agent")
            )

            # If resuming, get dates from saved state
            if resume:
                state = load_export_state(ws_name)
                if not state:
                    print(json.dumps({"error": "No export to resume"}))
                    sys.exit(1)
                from_date = state["config"]["from_date"]
                to_date = state["config"]["to_date"]
                output_file = state["config"]["output_file"]

            result = run_export(client, ws_name, from_date, to_date, output_file, resume)
            print(json.dumps({"ok": True, "status": result["status"]}, indent=2))

        except KeyboardInterrupt:
            print(json.dumps({"ok": True, "status": "paused", "message": "Use --resume to continue"}))
            sys.exit(0)
        except Exception as e:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
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
