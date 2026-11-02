# LamaScambaseBot

**A Telegram bot for community scam reports, account verification, identity tracking, and complaint moderation.**

LamaScambaseBot helps users search community-submitted reports, report suspicious activity, and track Telegram accounts through their numeric IDs and observed username history.

The project includes an administrative dashboard, complaint moderation, support tickets, broadcast messaging, and Telegram-native interface elements.

**Python 3.11+ · Telegram Bot API · SQLite · MIT-licensed**

---

## Features

### Account Lookup & Reports
- Search published reports by Telegram username.
- Search by numeric Telegram ID when identity information is available.
- Look up previously observed usernames.
- View detailed reports and their moderation status.
- Submit reports with descriptions and supporting evidence.
- Appeal published reports and request corrections.

### Telegram Identity Tracking
- Store Telegram IDs obtained from supported Telegram events.
- Track observed username changes over time.
- Accept account selection through Telegram's native user-sharing interface.
- Extract sender IDs from forwarded messages when Telegram exposes them.
- Preserve historical username associations.
- Review ambiguous identity matches before linking them to reports.
- Prevent reports from automatically transferring to a new owner of a previously used username.

**Important:** Telegram usernames are not permanent identifiers. The standard Telegram Bot API cannot resolve the ID of an arbitrary account from its username alone. Optional MTProto integration can resolve some current usernames but cannot reliably reconstruct their former owners.

### Administration & Moderation
- Review and approve submitted reports.
- Manage published and disputed reports.
- Inspect Telegram ID candidates and identity history.
- Review identity-related changes.
- Access administrative tools with server-side permission checks.
- Manage support tickets and user communications.

### Support System
- Create support tickets directly inside Telegram.
- Maintain persistent conversation history.
- Allow administrators to reply to individual tickets.
- Close and reopen support requests.
- Prevent unauthorized access to other users' tickets.

### Broadcast Messaging
- Create announcements through the administrative interface.
- Preview messages before sending.
- Preserve supported Telegram text formatting and custom emoji.
- Add URL buttons and internal navigation buttons.
- Pause, resume, and stop broadcast queues.
- Handle Telegram rate limits and delivery errors.
- Allow users to unsubscribe from optional news broadcasts.

### Telegram-Native Interface
- TgAndroidIcons custom emoji integration.
- Native Telegram button colors where supported.
- Interactive menus and inline keyboards.
- Clear navigation and contextual actions.
- Text fallbacks when premium interface elements are unavailable.

---

## Technology Stack

| Component | Technology |
|---|---|
| Language | Python 3.11+ |
| Telegram integration | pyTelegramBotAPI |
| Optional identity resolution | Telethon (MTProto) |
| Database | SQLite |
| Configuration | python-dotenv |
| HTTP requests | Requests |
| Background execution | Python workers |
| Deployment | Linux / systemd |

---

## Installation

### Requirements

Before installing, make sure you have:

- Python 3.11 or newer
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your numeric Telegram user ID for administrator access
- Internet access to Telegram services

### 1. Clone the repository

```bash
git clone https://github.com/ICEOF7/LamaScambaseBot.git
cd LamaScambaseBot
```

### 2. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

On Windows:

```powershell
python -m venv .venv
.venv\Scripts\activate
```

### 3. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 4. Configure the bot

Create a private environment file using the supplied example:

```bash
cp .env.example .env
```

Edit `.env` and configure the required values:

```dotenv
API_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
ADMIN_ID=YOUR_TELEGRAM_USER_ID

DB_PATH=data/reports.sqlite3
CHANNEL_USERNAMES=
```

The `.env.example` file contains additional optional settings for moderation, appearance, username lookups, identity synchronization, and broadcasts.

Never commit your actual `.env` file.

### 5. Start the bot

```bash
python app.py
```

The bot uses Telegram long polling. Do not run multiple polling instances with the same token.

For Linux servers, a sample systemd configuration is available at:

`deploy/lamascambase.service.example`

Review and adapt its paths, permissions, and service user before deploying it.

---

## Telegram ID System

LamaScambaseBot treats Telegram's numeric user ID as the primary identifier whenever a reliable association is available.

A Telegram username can be changed, deleted, or reassigned. For this reason, historical username matches are treated carefully.

### Supported identity sources

| Source | Availability |
|---|---|
| A user interacts with the bot | Supported |
| Telegram account selection | Supported |
| Forwarded message with visible sender | Supported |
| Previously observed account history | Supported |
| Arbitrary username lookup through Bot API | Not supported |
| Optional MTProto username resolution | Requires API credentials |

### Optional MTProto integration

You can configure:

```dotenv
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
```

These credentials are obtained from [my.telegram.org](https://my.telegram.org).

MTProto integration is optional. Core bot functionality remains available without it.

Identity synchronization must respect Telegram rate limits. A match involving a historical username must not be treated as confirmed ownership without sufficient evidence.

---

## Bot Commands

| Command | Description |
|---|---|
| `/start` | Open the main menu |
| `/check` | Search reports |
| `/list` | Browse published reports |
| `/report` | Submit a new report |
| `/myid` | Display your Telegram ID |
| `/clarify #123` | Provide identity information for your existing report |
| `/appeal` | Appeal a report |
| `/support` | Open the support section |
| `/tickets` | View support tickets |
| `/notifications` | Manage news notifications |
| `/unsubscribe` | Unsubscribe from news broadcasts |
| `/privacy` | Access privacy-related options |

Additional administrative functions are restricted to authorized administrators.

---

## Database & Data Safety

LamaScambaseBot uses SQLite to store reports, identity observations, moderation information, support tickets, and other operational records.

For a new installation, the default database location is:

`data/reports.sqlite3`

The repository intentionally excludes production databases, user information, private credentials, Telegram sessions, uploaded evidence, and runtime logs.

### Updating an existing installation

Before upgrading:

1. Stop the running bot.
2. Back up the existing SQLite database using a consistent backup method.
3. Preserve your private environment configuration and runtime files.
4. Test database migrations against a backup copy.
5. Deploy the updated source code.
6. Restart the existing service and verify its logs.

Never replace an existing production database with a fresh empty database during an upgrade.

---

## Testing

Run the automated test suite:

```bash
python -m unittest discover -s tests -v
```

Check compatibility with the installed Telegram library:

```bash
python scripts/check_telegram_contract.py
python scripts/check_hybrid_contract.py
python scripts/check_identity_dispatch.py
```

Check the source tree for common private files and credentials:

```bash
python scripts/check_release.py
```

Inspect Telegram ID coverage in an existing database without modifying its contents:

```bash
python scripts/identity_coverage.py --db data/reports.sqlite3
```

The source scanner is an additional safety check, not a replacement for reviewing files and Git history before publication.

Automated tests do not guarantee successful delivery through the live Telegram API.

---

## Security & Privacy

LamaScambaseBot processes user-submitted reports and potentially sensitive information.

When operating your own instance:

- Never expose bot tokens, Telegram API credentials, or database files.
- Restrict administrative access.
- Keep dependencies updated.
- Back up production data securely.
- Review reports before making them public.
- Allow incorrect or disputed information to be reviewed and corrected.
- Avoid publishing personally identifiable information unnecessarily.
- Follow applicable privacy laws and Telegram's platform rules.

**A published report is a user-submitted allegation, not proof of fraud, misconduct, or guilt.**

Operators are responsible for moderation decisions, data retention, and handling privacy or correction requests.

---

## Project Structure

```text
LamaScambaseBot/
├── app.py
├── community.py
├── community_store.py
├── delivery.py
├── hybrid_identity.py
├── identity_db.py
├── identity_maintenance.py
├── telegram_identity.py
├── ui.py
├── texts.py
├── requirements.txt
├── .env.example
├── .gitignore
├── deploy/
├── scripts/
└── tests/
```

The repository includes additional modules for identity diagnostics, reporting, runtime safety, and message processing.

---

## Contributing

Contributions, bug reports, and improvements are welcome.

If you want to contribute:

1. Fork the repository.
2. Create a separate branch.
3. Implement and test your changes.
4. Submit a pull request explaining your changes.

Do not include real Telegram tokens, personal data, production databases, or private user messages in issues or pull requests.

---

## License

See the [LICENSE](LICENSE) file for the project's licensing terms.

---

## Disclaimer

LamaScambaseBot is a community reporting and moderation tool. It does not independently establish whether an individual or account has committed fraud.

All reported information should be reviewed carefully. Historical usernames and unverified identity matches must not be treated as definitive identification.

---

**Developed and maintained by [ICEOF7](https://github.com/ICEOF7).**
