---
name: slack
description: This skill should be used when the user asks to "send a Slack message", "post to Slack", "DM someone on Slack", "reply to a thread", "check Slack notifications", "read my Slack messages", "what did I miss on Slack", "get my sent messages", "search Slack history", "find messages I sent", "give me a Slack digest", "Slack activity summary", "what happened on Slack last week", "what did I do on Slack", or mentions Slack messaging, notifications, or message history. Handles sending messages, reading notifications, generating activity digests, and searching message history via a Python client.
---

# Slack Integration Skill

This skill provides workflows for interacting with Slack via a self-contained Python client. It supports four primary workflows: sending messages, reading notifications, generating activity digests, and retrieving sent message history.

## Setup

### First-Time Setup Flow

If `config.json` is missing when running a Slack command, walk the user through setup:

1. **Create config files:**
   ```bash
   cd ~/.claude/skills/slack
   cp config.example.json config.json
   cp slack-cache.example.json slack-cache.json
   ```

2. **Guide user to extract browser tokens** (see `references/setup-guide.md`):
   - Open Slack in browser and log in
   - Open Developer Tools (F12 or Cmd+Option+I)
   - Get `xoxc_token` from localStorage (Application → Local Storage → look for `xoxc-`)
   - Get `xoxd_token` from cookies (Application → Cookies → cookie named `d`)
   - Get `user_agent` from console: `navigator.userAgent`

3. **Ask for preferences using AskUserQuestion:**
   ```
   question: "How would you like Slack links to open?"
   header: "Link style"
   options:
     - label: "Browser"
       description: "Open links in web browser"
     - label: "Native app"
       description: "Open links in Slack desktop app"
   multiSelect: false
   ```

4. **Write config.json with all values:**
   ```json
   {
     "xoxc_token": "<user's token>",
     "xoxd_token": "<user's token>",
     "user_agent": "<user's user agent>",
     "link_style": "browser" or "app"
   }
   ```

5. **Test the connection:**
   ```bash
   python3 ~/.claude/skills/slack/scripts/slack_client.py auth
   ```

### Config File Structure

`config.json` contains credentials and preferences:

```json
{
  "xoxc_token": "xoxc-...",
  "xoxd_token": "xoxd-...",
  "user_agent": "Mozilla/5.0...",
  "link_style": "app"
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `xoxc_token` | Yes | Browser token from localStorage |
| `xoxd_token` | Yes | Browser cookie token |
| `user_agent` | Yes | Your browser's User-Agent string |
| `link_style` | Yes | `"app"` (native Slack) or `"browser"` (web browser) |

### Manual Setup

If setting up manually:

```bash
cd ~/.claude/skills/slack
cp config.example.json config.json
cp slack-cache.example.json slack-cache.json
```

Edit `config.json` with your tokens and preferences. Refer to `references/setup-guide.md` for token extraction.

### Test the Connection

```bash
python3 ~/.claude/skills/slack/scripts/slack_client.py auth
```

## Python Client Commands

The `scripts/slack_client.py` script provides these commands:

| Command | Arguments | Purpose |
|---------|-----------|---------|
| `auth` | - | Test authentication, get user info |
| `channels` | [types] | List channels (default: all types) |
| `users` | - | List all workspace users |
| `history` | channel_id [limit] | Get message history |
| `replies` | channel_id thread_ts | Get thread replies |
| `search` | query [count] | Search messages |
| `send` | channel_id text [thread_ts] | Send a message |
| `permalink` | channel_id message_ts [workspace] | Get message permalink |

### Example Usage

```bash
SCRIPT=~/.claude/skills/slack/scripts/slack_client.py

# List public channels
python3 $SCRIPT channels "public_channel"

# Search for messages
python3 $SCRIPT search "from:@username after:2025-01-01" 50

# Send a message
python3 $SCRIPT send "C0123456789" "Hello world!"

# Send a thread reply
python3 $SCRIPT send "C0123456789" "Thread reply" "1234567890.123456"

# Get channel history
python3 $SCRIPT history "C0123456789" 20

# Get message permalink
python3 $SCRIPT permalink "C0123456789" "1234567890.123456"
```

## Performance Cache

A cache file (`slack-cache.json`) stores frequently-used IDs to reduce API calls.

### Using the Cache

Before making API calls to look up users or channels:
1. Read `slack-cache.json` in this skill folder
2. Check if the needed ID is already cached
3. If found, use the cached value directly
4. If not found, make the API call, then update the cache

### Cache Structure

```json
{
  "user": {"id": "...", "username": "...", "display_name": "..."},
  "self_dm_channel": "D...",
  "workspace": "your-workspace",
  "frequent_contacts": {"username": {"id": "...", "display_name": "..."}},
  "channels": {"#channel-name": "C..."}
}
```

### Updating the Cache

After successful lookups, add new entries:
- New user lookups → add to `frequent_contacts`
- New channel lookups → add to `channels`
- Update `last_updated` date

On lookup errors (user not found, channel not found), the cached entry may be stale - remove it and retry.

## Workflow 1: Send a Message

### To a Channel

1. Find the channel ID (check cache or list channels):
   ```bash
   python3 $SCRIPT channels "public_channel,private_channel"
   ```

2. Send the message:
   ```bash
   python3 $SCRIPT send "C0123456789" "Your message here"
   ```

### To a Thread

1. Get the thread's parent message timestamp (`ts`)
2. Send the reply:
   ```bash
   python3 $SCRIPT send "C0123456789" "Thread reply" "1234567890.123456"
   ```

### To a DM

1. Find the DM channel ID (check cache or use @username):
   ```bash
   python3 $SCRIPT channels "im"
   ```

2. Send the message:
   ```bash
   python3 $SCRIPT send "D0123456789" "Your DM message"
   ```

### Message Formatting

Messages support Slack's mrkdwn format:
- `*bold*` for bold text
- `_italic_` for italic text
- `~strikethrough~` for strikethrough
- `` `code` `` for inline code
- `<@USER_ID>` for user mentions
- `<#CHANNEL_ID>` for channel mentions

## Workflow 2: Read Recent Activity

To check what the user missed or review recent activity:

1. Get recent messages from relevant channels:
   ```bash
   python3 $SCRIPT history "C0123456789" 50
   ```

2. For each channel of interest, summarize:
   - New messages since last check
   - Mentions of the user
   - Important threads that need attention

3. Search for messages mentioning the user:
   ```bash
   python3 $SCRIPT search "<@USER_ID>" 20
   ```

## Workflow 3: Slack Activity Digest

When the user asks for a "Slack digest" or "activity summary", use the AskUserQuestion tool to prompt for the time period:

### Time Period Selection

**Note:** Slack search only supports date-based queries (`after:YYYY-MM-DD`), not datetime. Options are designed around this limitation.

Present these options using AskUserQuestion:

| Option | Search Query | Notes |
|--------|-------------|-------|
| Today only | `from:@username after:YYYY-MM-DD` (today's date) | Messages from today |
| Since yesterday | `from:@username after:YYYY-MM-DD` (yesterday's date) | Yesterday + today |
| Last 7 days | `from:@username after:YYYY-MM-DD` (7 days ago) | Full week |
| Last calendar week | `from:@username after:YYYY-MM-DD before:YYYY-MM-DD` | Mon-Sun of previous week |

### Generating the Digest

1. Search for user's sent messages in the selected period:
   ```bash
   python3 $SCRIPT search "from:@username after:2025-01-01" 100
   ```

2. Analyze messages and group by theme/conversation:
   - Identify main topics and projects discussed
   - Group related messages together
   - Note key people involved in each thread

3. **Build the message index** as you analyze:
   - Assign each referenced message a numbered ID (1.1, 1.2, 2.1, etc.)
   - First number = theme/section, second number = item within section
   - Store in `last-digest.json` (see format below)

4. Present digest using **numbered lists** (not bullets):

```markdown
## Your Slack Activity Digest: [Date Range]

### 1. [Theme/Project Name]

1.1. [Brief description of message/activity]
1.2. [Another message in this theme]
1.3. [Key decision or outcome]

People: [names involved]

### 2. [Theme/Project Name]

2.1. [Brief description]
2.2. [Another item]

People: [names involved]

### 3. Misc

3.1. [One-off message]
3.2. [Another minor item]

---

**Stats:** ~X messages | Channels: [list] | Busiest: [days]

💡 Say "open 1.2" to view any message in Slack
```

### Message Index File

After generating the digest, write `~/.claude/skills/slack/last-digest.json`:

```json
{
  "generated": "2025-01-15T14:30:00Z",
  "period": "2025-01-13 to 2025-01-15",
  "workspace": "hartreeworks",
  "messages": {
    "1.1": {"channel": "C04AFNMCNFP", "ts": "1736789012.123456"},
    "1.2": {"channel": "C04AFNMCNFP", "ts": "1736789100.654321"},
    "2.1": {"channel": "D18U650RY", "ts": "1736801234.111111"},
    "3.1": {"channel": "C02ABC123", "ts": "1736812345.222222"}
  }
}
```

### Opening Messages

When user says "open 1.2" or "open message 2.1":

1. Read `config.json` to get `link_style` preference
2. Read `slack-cache.json` to get `workspace`
3. Read `last-digest.json` and look up the message reference
4. Generate permalink using the `permalink` command with the user's link_style:
   ```bash
   # For link_style: "app" (default)
   python3 $SCRIPT permalink "C04AFNMCNFP" "1736789100.654321" "hartreeworks" "app"
   # Returns: https://hartreeworks.slack.com/archives/C04AFNMCNFP/p1736789100654321

   # For link_style: "browser"
   python3 $SCRIPT permalink "C04AFNMCNFP" "1736789100.654321" "hartreeworks" "browser"
   # Returns: https://hartreeworks.slack.com/messages/C04AFNMCNFP/p1736789100654321
   ```
5. Open the link:
   ```bash
   open "<permalink>"
   ```

### Example AskUserQuestion Call

```
Use AskUserQuestion with:
- question: "What time period would you like the Slack digest for?"
- header: "Time period"
- options:
  - label: "Today only"
    description: "Messages from today"
  - label: "Since yesterday"
    description: "Yesterday and today"
  - label: "Last 7 days"
    description: "Messages from the past week"
  - label: "Last calendar week"
    description: "Monday to Sunday of last week"
- multiSelect: false
```

## Workflow 4: Get Sent Messages

To retrieve messages the user sent during a specific period:

1. Search for the user's messages:
   ```bash
   python3 $SCRIPT search "from:@username" 50
   ```

2. Filter by date range if specified:
   ```bash
   python3 $SCRIPT search "from:@username after:2025-01-01 before:2025-01-31" 100
   ```

3. Present results grouped by channel or date.

### Common Search Queries

| Query | Purpose |
|-------|---------|
| `from:@username` | All messages from user |
| `from:@username in:#channel` | Messages in specific channel |
| `from:@username after:YYYY-MM-DD` | Messages after date |
| `from:@username before:YYYY-MM-DD` | Messages before date |
| `from:@username has:link` | Messages containing links |
| `from:@username has:reaction` | Messages with reactions |

## Working with Threads

### Fetch Thread Replies

```bash
python3 $SCRIPT replies "C0123456789" "1234567890.123456"
```

### Reply to a Thread

```bash
python3 $SCRIPT send "C0123456789" "Your reply" "1234567890.123456"
```

## Channel Discovery

### List All Channels

```bash
python3 $SCRIPT channels "public_channel,private_channel"
```

### List DM Conversations

```bash
python3 $SCRIPT channels "im,mpim"
```

### List All Users

```bash
python3 $SCRIPT users
```

## Error Handling

### Common Issues

| Error | Cause | Solution |
|-------|-------|----------|
| `"ok": false` | API error | Check the `error` field in response |
| `invalid_auth` | Token expired | Extract fresh tokens from browser |
| `channel_not_found` | Invalid channel ID | List channels to verify |
| `not_in_channel` | User not a member | Join channel first |

### Token Expiry

Browser tokens (xoxc/xoxd) expire periodically. If requests fail with auth errors:
1. Extract fresh tokens from browser
2. Update `config.json`
3. Test with `python3 $SCRIPT auth`

## Known Limitations

### No Activity Feed / Unread Notifications

The Slack Activity feed API is not available. Cannot directly fetch:

- Unread notification count
- Reactions to user's messages

Workarounds:
- Search for messages mentioning the user's ID: `<@USER_ID>`
- Search for recent activity in user's DMs
- Check specific channels for recent messages

## Additional Resources

### Reference Files

For detailed API response formats:
- **`references/tools-reference.md`** - Complete API documentation

### Token Setup

For extracting browser tokens:
- **`references/setup-guide.md`** - Step-by-step token extraction guide
