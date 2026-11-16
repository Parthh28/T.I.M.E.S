# T.I.M.E.S — Telegram Integrated Management & Event Scheduler

A personal Telegram Calendar & Task Assistant that understands natural-language requests, synchronizes time-bound events with **Google Calendar**, manages to-dos and deliverables with **Notion**, dispatches unified notifications, and checks for schedule conflicts with smart resolution.

## Features

- 🗣️ **Unified Natural Language** — Powered by Gemini AI. Classifies queries dynamically into Calendar Events, Notion Tasks, Combined Actions, or Conversational inquiries.
- 📅 **Google Calendar Sync** — Creates, reschedules, and deletes events directly in Google Calendar.
- 📝 **Notion Task Management** — Full task lifecycle (create, retrieve, prioritize, categorize, complete, and delete) backed by Notion databases.
- 🔗 **Combined Actions** — Single-prompt workflows: creates a calendar slot and a related Notion task in one confirmation prompt.
- ⚠️ **Conflict Detection & Smart Suggestions** — Real-time Google Calendar `freebusy` overlap checks with 1-click alternative slot suggestions.
- 🌅 **Daily Morning Briefing** — Automated 8:00 AM morning digest of your day's schedule & meeting links.
- 🎙️ **Spoken Phone Voice Notes** — Automatically speaks to your phone via Telegram audio notes (*"The event is gonna start now: [Title]"*) when events begin.
- ⏰ **Shared Multi-Tier Reminders** — APScheduler + SQLite reminder engine with persistent alerts for both Google Calendar events and Notion tasks.
- 🛡️ **Mandatory Confirmation & Clarification** — Inline Telegram buttons (`✅ Confirm` / `❌ Cancel`) protect all create, update, complete, and delete actions. Ambiguous requests trigger clarification questions.
- 🌐 **Web Event Scraping** — Paste event links (Luma, Eventbrite, etc.) and auto-extract calendar details.
- 🔒 **Secure** — Restricted to your authorized Telegram account only.

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message, bot overview, and available features |
| `/tasks` | View active and upcoming Notion tasks grouped by priority |
| `/briefing` | Today's morning agenda, meeting links & schedule summary |
| `/upcoming` | Show upcoming Google Calendar events (next 3 weeks, birthday-filtered) |
| `/today` | Show today's calendar events |
| `/reminders` | Show scheduled Telegram notification timings & countdowns |
| `/testvoice` | Send a test voice note to your phone |
| `/delete` | Delete an upcoming event with confirmation |
| `/new` | Step-by-step guided event creation |
| `/cancel` | Cancel the current operation |

Or just type natural-language instructions:
- **Calendar Event:** *"Schedule a meeting with Rahul on Friday at 3 PM"*
- **Notion Task:** *"Remind me to finish the quarterly budget report by Thursday, High priority, category Finance"*
- **Combined Action:** *"Schedule project sync tomorrow from 3 to 4 PM and add task prepare presentation slides"*
- **Mark Complete:** *"Mark quarterly budget report as completed"*
- **Task Deletion:** *"Delete task prepare presentation slides"*
- **Reschedule Event:** *"Move Literature fest to 2 PM"*
- **Delete Event:** *"Cancel my AWS event"*
- **Event Link Scraping:** *"Check this event out: https://lu.ma/tech-event"*

---

## Prerequisites

- **Python 3.11+** — [Download Python](https://www.python.org/downloads/)
- **Telegram Account** — For creating and using the bot
- **Google Account** — For Calendar access
- **Google Cloud Project** — For Calendar API and OAuth credentials
- **Gemini API Key** — For natural language processing
- **Notion Account & Database** — For personal task management and deliverables

---

## Setup Guide

### 1. Python Installation

1. Download Python 3.11+ from [python.org](https://www.python.org/downloads/)
2. During installation, check **"Add Python to PATH"**
3. Verify: `python --version`

### 2. Clone and Set Up Virtual Environment

```bash
# Clone the repository
git clone <your-repo-url>
cd T.I.M.E.S-Telegram-Bot

# Create a virtual environment
python -m venv venv

# Activate it
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Create a Telegram Bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Choose a name (e.g., "TIMES Calendar Assistant")
4. Choose a username (must end in "bot", e.g., `times_calendar_bot`)
5. Copy the **HTTP API token** — you'll need this for `TELEGRAM_BOT_TOKEN`

**Find your Telegram User ID:**
1. Search for **@userinfobot** on Telegram
2. Send `/start`
3. Copy your **User ID** — you'll need this for `AUTHORIZED_USER_ID`

### 4. Obtain a Gemini API Key

1. Go to [Google AI Studio](https://aistudio.google.com/app/apikey)
2. Sign in with your Google account
3. Click **"Create API Key"**
4. Copy the key — you'll need this for `GEMINI_API_KEY`

### 5. Configure Google Calendar API & OAuth

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select an existing one)
3. Navigate to **APIs & Services > Library**
4. Search for **"Google Calendar API"** and click **Enable**
5. Go to **APIs & Services > Credentials**
6. Click **"+ Create Credentials" > "OAuth client ID"**
7. If prompted, configure the OAuth consent screen:
   - Choose **External** user type
   - Fill in the app name and your email
   - Add scope: `https://www.googleapis.com/auth/calendar`
   - Add your email as a test user
8. For Application Type, choose **"Desktop app"**
9. Click **Create**
10. Download the JSON file and save it as **`credentials.json`** in the project root

### 6. Set Up Notion API Integration

1. Go to [Notion Developers Integrations](https://www.notion.so/my-integrations).
2. Click **"+ New integration"**.
3. Choose a name (e.g. `TIMES Bot`), associate it with your Notion workspace, and ensure **Read content**, **Update content**, and **Insert content** permissions are granted.
4. Copy the **Internal Integration Secret** (`secret_...`) — you will need this for `NOTION_API_KEY`.
5. In your Notion workspace, create a database for Tasks (e.g., Table or Board layout).
   Ensure the following properties exist (the bot automatically detects case-insensitive variations and property types):
   - **Name** or **Task** (`title`)
   - **Status** (`status` or `select`: options: `Not started`, `In progress`, `Done`)
   - **Priority** (`select`: options: `High`, `Medium`, `Low`)
   - **Category** (`select`: e.g. `Work`, `Personal`, `Study`, `Finance`, `Health`)
   - **Due Date** (`date`)
   - **Notes** or **Description** (`rich_text`)
6. **Share the Database with your Integration**:
   - Open your task database page in Notion.
   - Click the **`···`** menu (top right) > **Connect to** > select your integration name (`TIMES Bot`).
7. **Find your Database ID**:
   - Copy the database URL: `https://www.notion.so/{workspace_name}/{database_id}?v=...`
   - The 32-character hexadecimal string between the slash and the query parameters is your `NOTION_DATABASE_ID`.

### 7. Configure Environment Variables

```bash
# Copy the example file
cp .env.example .env
```

Edit `.env` with your actual values:

```env
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
AUTHORIZED_USER_ID=123456789
GEMINI_API_KEY=AIzaSy...
GEMINI_MODEL=gemini-2.5-flash
GOOGLE_CREDENTIALS_FILE=credentials.json
GOOGLE_TOKEN_FILE=token.json
NOTION_API_KEY=secret_...
NOTION_DATABASE_ID=...
DATABASE_FILE=calendar_bot.db
TIMEZONE=Asia/Kolkata
LOG_LEVEL=INFO
```

### 8. Run the Bot

```bash
python bot.py
```

### 9. Authorize Google Calendar (First Run)

On the first run, the bot will:
1. Open a browser window for Google OAuth sign-in
2. Ask you to sign in with your Google account
3. Request permission to access your Google Calendar
4. After authorization, the token is saved to `token.json`

> **Note:** This step requires running the bot on a machine with a web browser. After initial authorization, the token file can be transferred to a server.

---

## Testing Every Feature

### Test Natural Language Event Creation
1. Open your bot in Telegram
2. Type: `Schedule a meeting tomorrow at 3 PM for 1 hour`
3. Verify the event summary is shown with correct details
4. Click **✅ Confirm** to create the event
5. Check your Google Calendar to verify

### Test Guided Event Creation
1. Send `/new`
2. Enter the event title when prompted
3. Enter the date (e.g., `2026-11-15` or `next Monday`)
4. Enter the start time (e.g., `10:00` or `3 PM`)
5. Enter the duration (e.g., `1 hour` or `14:30` for end time)
6. Select a reminder option
7. Confirm or cancel the event

### Test Viewing Events
- Send `/upcoming` to see the next 10 events
- Send `/today` to see today's events

### Test Notion Task Management
1. **Create Task:** Type `Remind me to submit project report by Friday, High priority, category Study`
2. **Review & Confirm:** Verify task details and click **✅ Confirm**
3. **View Tasks:** Send `/tasks` to list your active to-dos grouped by priority
4. **Mark Done:** Type `Mark submit project report as completed` and confirm
5. **Delete Task:** Type `Delete task submit project report` and confirm

### Test Combined Action
1. Type: `Schedule team sync tomorrow from 3 to 4 PM and create task prepare sync agenda`
2. Inspect the unified confirmation prompt presenting both the calendar slot and Notion task
3. Click **✅ Confirm Both** to execute both simultaneously

### Test Reminders
1. Create an event or task with a reminder (e.g., 10 minutes before)
2. Keep the bot running
3. Wait for the reminder Telegram message

### Test Cancellation
- Send `/cancel` during any guided flow
- Click **❌ Cancel** on any confirmation screen

---

## Running Unit Tests

```bash
# Run all tests
python -m pytest tests/ -v

# Or with unittest
python -m unittest discover tests/ -v

# Run specific test files
python -m pytest tests/test_intent_router.py -v
python -m pytest tests/test_task_parser.py -v
python -m pytest tests/test_notion.py -v
python -m pytest tests/test_database.py -v
python -m pytest tests/test_event_parser.py -v
python -m pytest tests/test_conflict_detection.py -v
```

> **Note:** Unit tests use mocked APIs and do not require real API keys. The tests cover intent routing, event & task parsing, database operations, conflict detection, and utility functions.

### Testing Limitations
- **Gemini API integration tests** require a valid `GEMINI_API_KEY`
- **Google Calendar integration tests** require valid OAuth credentials
- **Telegram bot integration tests** require a real bot token and active Telegram session
- All of the above are mocked in unit tests to run without credentials

---

## Troubleshooting

### "Required environment variable 'X' is not set"
- Make sure you've copied `.env.example` to `.env`
- Fill in all required values

### Google OAuth Errors
- Verify `credentials.json` is in the project root
- Delete `token.json` and re-authorize
- Make sure Calendar API is enabled in Google Cloud Console
- Check that your email is added as a test user in OAuth consent screen

### "Unauthorized access attempt"
- Verify `AUTHORIZED_USER_ID` in `.env` matches your Telegram user ID
- Use @userinfobot to confirm your user ID

### Gemini API Errors
- Check your API key is valid
- Verify you haven't exceeded rate limits
- Try a different model (e.g., `gemini-2.5-flash`)

### Bot Not Responding
- Check the terminal for error messages
- Verify the bot token is correct
- Make sure no other bot instance is running with the same token

### Reminders Not Working
- The bot process must be continuously running
- Check that APScheduler started (look for "APScheduler started" in logs)
- Verify the event hasn't already passed

---

## Security Precautions

- ⚠️ **Never share your `.env` file or commit it to Git**
- ⚠️ **Never share `credentials.json` or `token.json`**
- ⚠️ **The bot only responds to the configured `AUTHORIZED_USER_ID`**
- ⚠️ **API keys and tokens are never logged**
- ⚠️ **The `.gitignore` blocks all sensitive files from being committed**

---

## Local Hosting Limitations

- **Reminders require the bot process to be running.** If the bot stops, reminders scheduled during downtime will NOT be sent until the bot restarts.
- **On restart,** the bot reconciles pending reminders:
  - If the event hasn't started yet, late reminders are sent immediately
  - If the event has already passed, reminders are marked as overdue
- **OAuth authorization** requires a browser on first run

---

## Deployment for Reliable Reminders

For 24/7 reliable reminders, deploy the bot to:

1. **VPS (Recommended):** DigitalOcean, Linode, AWS EC2
   ```bash
   # Use screen or tmux to keep it running
   screen -S calendar-bot
   python bot.py
   # Ctrl+A, then D to detach
   ```

2. **systemd Service (Linux):**
   ```ini
   [Unit]
   Description=TIMES Calendar Bot
   After=network.target

   [Service]
   Type=simple
   User=your-user
   WorkingDirectory=/path/to/T.I.M.E.S-Telegram-Bot
   ExecStart=/path/to/venv/bin/python bot.py
   Restart=always
   RestartSec=10

   [Install]
   WantedBy=multi-user.target
   ```

3. **Docker:**
   ```dockerfile
   FROM python:3.11-slim
   WORKDIR /app
   COPY requirements.txt .
   RUN pip install -r requirements.txt
   COPY . .
   CMD ["python", "bot.py"]
   ```

> **Important:** Transfer `token.json` to the server after completing OAuth locally, or run OAuth on the server if it has a browser.

---

## Project Structure

```
T.I.M.E.S-Telegram-Bot/
├── bot.py                  # Main Telegram bot (commands, callbacks, unified intent dispatch)
├── config.py               # Environment variables, constants, Notion credentials, logging
├── gemini_service.py       # Gemini AI unified intent router (events, tasks, combined actions)
├── calendar_service.py     # Google Calendar API (OAuth, CRUD, conflict detection)
├── notion_service.py       # Asynchronous Notion API integration (CRUD & schema discovery)
├── event_parser.py         # Calendar event validation and normalization
├── task_parser.py          # Notion task validation, priority & category formatting
├── reminder_service.py     # Unified APScheduler engine for Calendar events and Notion tasks
├── database.py             # SQLite schema (events, tasks, unified reminders, confirmations)
├── requirements.txt        # Pinned Python dependencies
├── .env.example            # Environment variable template
├── .gitignore              # Files excluded from Git
├── README.md               # Documentation and setup instructions
└── tests/
    ├── __init__.py
    ├── test_intent_router.py      # Gemini intent classification tests
    ├── test_task_parser.py        # Task parsing, validation, and priority formatting tests
    ├── test_notion.py             # Notion service schema and payload tests
    ├── test_database.py           # Database CRUD, migration, and reminder constraint tests
    ├── test_event_parser.py       # Calendar event parsing and validation tests
    ├── test_conflict_detection.py # Calendar freebusy slot overlap tests
    └── test_bot.py                # Bot utility and authorization tests
```

---

## License

This project is for personal use. See LICENSE file for details.
