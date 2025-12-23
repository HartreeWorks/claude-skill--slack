#!/usr/bin/env python3
"""
Slack client using browser tokens (xoxc/xoxd) for stealth mode.
Replaces the third-party slack-mcp-server npm package.
"""

import requests
import json
import sys
import os
from pathlib import Path
from urllib.parse import urlencode

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


def load_config() -> dict:
    """Load credentials from config.json in the skill folder."""
    config_path = Path(__file__).parent.parent / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")
    with open(config_path) as f:
        return json.load(f)


def main():
    """CLI interface for the Slack client."""
    if len(sys.argv) < 2:
        print(json.dumps({
            "error": "No command provided",
            "usage": "slack_client.py <command> [args]",
            "commands": {
                "auth": "Test authentication",
                "channels": "List channels (optional: types)",
                "users": "List users",
                "history": "Get channel history (channel_id, optional: limit)",
                "replies": "Get thread replies (channel_id, thread_ts)",
                "search": "Search messages (query, optional: count)",
                "send": "Send message (channel_id, text, optional: thread_ts)",
                "permalink": "Get message permalink (channel_id, message_ts, optional: workspace, link_style)"
            }
        }, indent=2))
        sys.exit(1)

    # Load config and create client
    try:
        config = load_config()
        client = SlackClient(
            config["xoxc_token"],
            config["xoxd_token"],
            config.get("user_agent")
        )
    except Exception as e:
        print(json.dumps({"error": f"Failed to load config: {e}"}))
        sys.exit(1)

    command = sys.argv[1]
    args = sys.argv[2:]

    try:
        if command == "auth":
            result = client.auth_test()

        elif command == "channels":
            types = args[0] if args else "public_channel,private_channel,im,mpim"
            result = client.channels_list(types=types)

        elif command == "users":
            result = client.users_list()

        elif command == "history":
            if not args:
                print(json.dumps({"error": "channel_id required"}))
                sys.exit(1)
            channel = args[0]
            limit = int(args[1]) if len(args) > 1 else 100
            result = client.conversations_history(channel, limit)

        elif command == "replies":
            if len(args) < 2:
                print(json.dumps({"error": "channel_id and thread_ts required"}))
                sys.exit(1)
            result = client.conversations_replies(args[0], args[1])

        elif command == "search":
            if not args:
                print(json.dumps({"error": "query required"}))
                sys.exit(1)
            query = args[0]
            count = int(args[1]) if len(args) > 1 else 20
            result = client.search_messages(query, count)

        elif command == "send":
            if len(args) < 2:
                print(json.dumps({"error": "channel_id and text required"}))
                sys.exit(1)
            channel = args[0]
            text = args[1]
            thread_ts = args[2] if len(args) > 2 else None
            result = client.post_message(channel, text, thread_ts)

        elif command == "permalink":
            if len(args) < 2:
                print(json.dumps({"error": "channel_id and message_ts required"}))
                sys.exit(1)
            channel = args[0]
            message_ts = args[1]
            workspace = args[2] if len(args) > 2 else None
            link_style = args[3] if len(args) > 3 else "app"
            permalink = client.get_permalink(channel, message_ts, workspace, link_style)
            result = {"ok": True, "permalink": permalink}

        else:
            print(json.dumps({"error": f"Unknown command: {command}"}))
            sys.exit(1)

        print(json.dumps(result, indent=2))

    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
