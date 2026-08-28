```
   _____ _               _     _   _             _
  / ____| |             | |   | | | |           | |
 | |  __| |__   ___  ___| |_  | |_| |_   _ _ __ | |_ ___ _ __
 | | |_ | '_ \ / _ \/ __| __| |  _  | | | | '_ \| __/ _ \ '__|
 | |__| | | | | (_) \__ \ |_  | | | | |_| | | | | ||  __/ |
  \_____|_| |_|\___/|___/\__| |_| |_|\__,_|_| |_|\__\___|_|
```
<div align="center">
**Automated AI Bug Reproduction Engine**

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)]()

<a href="https://www.producthunt.com/products/ghost-hunter-2?embed=true&amp;utm_source=badge-featured&amp;utm_medium=badge&amp;utm_campaign=badge-ghost-hunter-2" target="_blank" rel="noopener noreferrer"><img alt="Ghost Hunter - Turn bug reports into reproduced crashes, automatically. | Product Hunt" width="250" height="54" src="https://api.producthunt.com/widgets/embed-image/v1/featured.svg?post_id=1234561&amp;theme=neutral&amp;t=1787909076478"></a>

*Stop chasing ghosts. Start reproducing them.*

</div>

<br>

# Ghost Hunter 👻

Ghost Hunter is an autonomous, AI-driven DevOps CLI tool that automatically reproduces bug reports submitted on GitHub. When a user creates an issue or comments `bot/reproduce`, Ghost Hunter intercepts the webhook, uses an LLM to determine the execution steps, spins up an isolated Docker sandbox, clones the repository, runs the code, captures the resulting logs, and automatically posts the crash report directly back to the GitHub issue.

---

## 🏗️ Architecture & Workflow

1. **Trigger:** A user comments `bot/reproduce` on a GitHub issue.
2. **Webhook Proxy:** GitHub fires a secure webhook. Since Ghost Hunter runs locally on your machine, it uses a reverse proxy (`smee.io`) to catch the webhook behind your firewall.
3. **Verification:** Ghost Hunter's local FastAPI server cryptographically verifies the webhook signature using a shared `WEBHOOK_SECRET` to prevent tampering.
4. **AI Parsing:** Ghost Hunter queries an LLM (via OpenRouter) using structured JSON to parse the issue and determine exactly how to trigger the bug.
5. **Docker Execution:** A secure background Docker container is spun up. The repository is cloned, dependencies are installed, and the reproduction commands are executed.
6. **Result Generation:** The stdout/stderr from the sandbox is evaluated to see if the expected crash occurred. 
7. **Automated Reply:** Ghost Hunter dynamically generates short-lived Installation Access Tokens (if configured as a GitHub App) to authenticate and post a friendly markdown summary back to the GitHub issue.

---

## 🛠️ Prerequisites

Before installing Ghost Hunter, ensure you have the following installed on your host machine:

1. **Python 3.10 or higher**
2. **Docker Desktop** (Must be actively running in the background)
3. **Git** 

---

## 📦 Installation Methods

Ghost Hunter is primarily a CLI tool. 

### Method 1: Local Source Install (Recommended for Developers)
1. Clone the repository:
   ```bash
   git clone https://github.com/qxmcu/ghost-hunter.git
   cd ghost-hunter
   ```
2. Install dependencies and the CLI globally using pip:
   ```bash
   pip install -e .
   ```
   *(This requires that the directory containing `ghost-hunter` remains on your computer).*

### Method 2: Global PyPI Install
You can install it directly from the repository via pip:
```bash
pip install git+https://github.com/qxmcu/ghost-hunter.git
```

Once installed via either method, the `ghost` command will be globally available in your terminal. Verify the installation:
```bash
ghost --version
```

---

## 🔑 Authentication & API Keys

Ghost Hunter requires a few highly secure keys to operate completely autonomously. It supports two modes of GitHub authentication: **Personal Access Tokens** (tested, recommended for now) and **GitHub App** (documented, not yet verified end-to-end).

Run the initialization wizard to begin setting up your keys:
```bash
ghost init
```

### 1. GitHub Authentication (Choose A or B)

#### Option A: Personal Access Token (Recommended — tested)
Fine-grained PATs work, but they **will expire** (max 1 year).
1. Go to **GitHub -> Developer Settings -> Personal Access Tokens -> Fine-grained tokens**.
2. Click **Generate new token**.
3. **Repository access:** Choose `Only select repositories` and explicitly select the repositories you want Ghost Hunter to monitor.
4. **Permissions:** Drop down `Repository permissions` -> Find `Issues` -> Set to **Read and write**. Find `Contents` -> Set to **Read-only**.
5. Generate the token, copy it, run `ghost profile edit pat`, and paste it.

#### Option B: GitHub App (Production Grade — not yet verified)
> ⚠️ **Untested:** This flow is documented based on GitHub's standard GitHub App setup process, but has not yet been run end-to-end against Ghost Hunter. If you try it, please open an issue with your results.

GitHub Apps are the standard approach for production bot automation. Their tokens never expire, and they authenticate securely using asymmetric RSA keys.
1. Go to **GitHub -> Developer Settings -> GitHub Apps -> New GitHub App**.
2. Give it a name (e.g., `MyGhostBot`) and a Homepage URL.
3. **Important Permissions:** Under "Repository permissions", grant **Read & Write** access to **Issues**. Grant **Read** access to **Contents**.
4. Disable "Webhook" for now (we will set it up in the next section), and click Create.
5. **Get the App ID:** At the top of the General page, copy your numeric **App ID**. Run `ghost profile edit appid` and paste it.
6. **Get the Private Key:** Scroll down and click **Generate a private key**. Download the `.pem` file. Open the `.pem` file in a text editor, copy everything (including `-----BEGIN RSA PRIVATE KEY-----`), run `ghost profile edit pat`, and paste it.

*In theory, Ghost Hunter should then generate its own short-lived JWTs and Installation Access Tokens on the fly — but this has not been confirmed working yet.*

#### Option C: GitHub App as webhook source + PAT for auth (tested)
This is a hybrid worth knowing about: you can create a GitHub App purely to be the source of the webhook delivery (instead of a repo-level webhook), while still authenticating API calls with your PAT rather than the App's own key. This has been tested and works.
1. Go to **GitHub -> Developer Settings -> GitHub Apps -> New GitHub App**.
2. Give it a name and Homepage URL.
3. Under **Webhook**, check "Active" and set the **Webhook URL** to your Smee.io Webhook Proxy URL (see the Webhook Proxy section below).
4. Set a **Webhook secret** and save it with `ghost profile edit webhook` (same as the repo-webhook flow).
5. Install the App on the repositories you want it to watch.
6. Continue using your PAT (Option A above) for `ghost profile edit pat` — the App is only responsible for delivering the webhook, not for authenticating Ghost Hunter's replies.

### 2. LLM Authentication (OpenRouter)
Ghost Hunter uses OpenRouter to flexibly route to the best LLMs.
1. Create an account at [OpenRouter.ai](https://openrouter.ai).
2. Generate an API Key.
3. Run `ghost profile edit api` and paste the key.

**Model Selection:** Ghost Hunter *strictly requires* an LLM that is capable of Structured JSON Outputs. 
- **Confirmed NOT to work well:** `nemotron-free` — frequently returns malformed JSON (missing/unbalanced brackets) or drifts off-task, triggering Ghost Hunter's fallback failsafe even when the underlying reproduction actually succeeded.
- **Candidates to try (not yet confirmed):** `openai/gpt-4o-mini` and `meta-llama/llama-3.1-8b-instruct:free` are reasonable options based on general JSON reliability, but have not been verified against Ghost Hunter specifically yet. If you test one, please open an issue with your results.

To set the model:
```bash
ghost profile edit model
```

---

## 🪝 Setting up the Webhook Proxy

Because your laptop sits behind a router firewall, GitHub cannot send webhooks directly to `localhost`. Ghost Hunter fixes this automatically using Smee.io.

1. Go to [smee.io](https://smee.io) and click **"Start a new channel"**.
2. Copy the **Webhook Proxy URL** (e.g., `https://smee.io/AbCdEfGhIj`).
3. Run `ghost profile edit url` and paste that URL into the CLI.
4. Now, go to your GitHub repository (or your GitHub App settings).
5. Add a Webhook, and paste that exact same Smee URL as the **Payload URL**.
6. **Set a Webhook Secret:** Type a random, secure password (e.g., `MySuperSecret123!`) into the **Webhook secret** box on GitHub and click Save.
7. Run `ghost profile edit webhook` in your terminal and paste that exact same secret.

*Ghost Hunter strictly enforces cryptographic signatures (`x-hub-signature-256`). If the secrets do not match exactly, the CLI will reject the payload with a `401 Unauthorized` to protect you from spoofed webhooks.*

---

## 🚀 Running the Engine

Once your keys and webhook are configured, start the engine:

```bash
ghost serve
```

You will see output indicating that the Smee Proxy has connected and the FastAPI server is running. Leave this terminal open.

### Triggering a Test
1. Go to your monitored repository on GitHub.
2. Open an issue formatted using the [Issue Template](#-issue-template-for-reliable-reproduction) below.
3. Type a comment containing the exact trigger phrase:
   ```text
   bot/reproduce
   ```
   **This exact phrase is required.** Ghost Hunter's webhook handler filters incoming comments for this literal string — a comment without it is ignored entirely, no matter how clearly the bug is described elsewhere in the issue.

   The match is a **case-insensitive substring check** (`"bot/reproduce" in comment_body.lower()`), so the phrase doesn't need to stand alone. All of the following will trigger it:
   - `bot/reproduce`
   - `please bot/reproduce this`
   - `Hey @Ghost, can you BOT/REPRODUCE this crash for me?`
4. Watch your terminal! You will see Ghost Hunter catch the webhook, parse the LLM, spin up Docker, execute your code, and automatically post the crash report back to GitHub!

---

## 📋 Issue Template for Reliable Reproduction

Ghost Hunter's LLM parsing step works best when issues follow a consistent structure. The template below has been tested and reliably produces correct reproductions:

```markdown
## Environment
- OS: Ubuntu 22.04
- Python: 3.11
- Dependencies from requirements.txt

## What happened
Running the app with an empty config file causes a KeyError crash immediately on startup.

## Steps to reproduce
1. Clone the repo
2. pip install -r requirements.txt
3. python main.py --config empty_config.json

## Error
Traceback (most recent call last):
  File "main.py", line 45, in <module>
    config = load_config(args.config)
  File "main.py", line 23, in load_config
    return data["settings"]
KeyError: 'settings'
```

**Sections that matter:**
- **Environment** — OS, language/runtime version, and how dependencies are installed. This is what the LLM uses to pick/configure the Docker base image.
- **Steps to reproduce** — a numbered, literal command sequence. The LLM turns this almost directly into the commands run inside the sandbox, so keep it concrete and copy-pasteable rather than descriptive.
- **Error** — the actual traceback/output, if you have it. Helps the LLM (and you) confirm the sandbox reproduced the *same* failure, not just *a* failure.

Comment `bot/reproduce` on the issue once it's filed to trigger the run.

> Note: this template reflects the format that's been used successfully so far. If you're using `ghost.yml` in your repo to pin the base image or environment explicitly, the "Environment" section in the issue becomes less critical since `ghost.yml` takes precedence.

---

## ⚙️ CLI Reference Commands

Ghost Hunter includes a robust configuration manager to edit keys individually without re-running the setup wizard.

| Command | Description |
|---|---|
| `ghost init` | Run the interactive setup wizard for first-time use. |
| `ghost serve [-p PORT]` | Start the FastAPI server and Smee proxy background thread. |
| `ghost profile edit [FIELD]` | Edit a specific configuration key. Allowed fields: `pat`, `api`, `model`, `url`, `webhook`, `appid`. |
| `ghost profile view` | View your currently active configuration (requires CLI password). |
| `ghost profile clean` | Intelligently strip trailing whitespaces and rogue formatting issues in your configuration file. |
| `ghost history [ISSUE]` | View the local SQLite history/audit logs for a specific issue number. |

---

## 🧠 Advanced Configuration (`ghost.yml`)

By default, Ghost Hunter is smart enough to figure out how to run your code autonomously. However, you can force explicit instructions by creating a `ghost.yml` file in the root of your target repository.

Example `ghost.yml`:
```yaml
base_image: "python:3.11-slim"
allowed_network_access: false
max_retries: 2
default_packages:
  - build-essential
env:
  TEST_MODE: "true"
```
Ghost Hunter will fetch this file via the GitHub API before every run and strictly merge it with the LLM's dynamically generated instructions.

---

## 🛡️ Security & Hardening

Ghost Hunter is built for zero-trust environments.
1. **Docker Isolation:** Every reproduction runs in a fresh, ephemeral Docker container. Ghost Hunter automatically deletes the container the moment the run completes.
2. **Deduplication:** The server tracks `X-GitHub-Delivery` IDs in a local SQLite WAL database. If GitHub sends a duplicate webhook, or if you accidentally click "Redeliver" on Smee.io, the engine intelligently ignores it to prevent infinite loops.
3. **Agentic Retry Loops:** If the LLM generates a bad Docker command or hallucinated package, Ghost Hunter captures the failure logs, injects them back into the LLM context window, and retries up to 3 times automatically.
4. **Webhook Validation:** Rejects any incoming payload that lacks a valid cryptographic HMAC SHA-256 signature.

---

## ⚠️ Limitations & Known Issues

- **Free-Tier LLM Parsing:** Ghost Hunter strictly relies on the LLM's ability to output valid, structured JSON. Free-tier or open-source models (e.g., `nemotron-free` on OpenRouter) are confirmed to sometimes silently fail JSON parsing or lose track of the task, which triggers the fallback failsafe even when the underlying reproduction actually succeeded. `openai/gpt-4o-mini` is expected to be more reliable based on general JSON-mode support, but has not yet been verified against Ghost Hunter specifically — see the [Authentication](#-authentication--api-keys) section.
- **Smee.io Disconnects:** Ghost Hunter holds an indefinite background connection to Smee.io to proxy webhooks. If your local internet drops, the proxy will time out, though it is engineered to automatically attempt reconnections.
- **GitHub App Auth Path:** The GitHub App JWT/Installation Token authentication flow (Option B) is documented but not yet verified end-to-end. The tested paths are PAT auth (Option A) and GitHub App-as-webhook-source with PAT auth (Option C).

---

## ❓ Troubleshooting

- **`401 Unauthorized` in terminal:** Your `WEBHOOK_SECRET` in GitHub does not match the secret in `ghost profile edit webhook`. Ensure you hit "Save" on GitHub!
- **`403 Forbidden` / `404 Not Found`:** Your GitHub authentication failed. If you are using a fine-grained PAT, it likely expired or lacks `Issues: Read & Write` permissions. Switch to a GitHub App (`appid` + `pat` PEM key) to resolve this permanently.
- **"LLM Parsing Failed" fallback comment posted:** You are using a weak LLM (like Nemotron-3 free tier) that does not support strictly adhering to `json_object` schemas. Change your model to `openai/gpt-4o-mini` using `ghost profile edit model`.

---

## 🤝 Contributing

We'd love your help! Issues and PRs are extremely welcome.

Because Ghost Hunter orchestrates multiple complex systems (Docker, LLMs, and GitHub Apps), if you are opening a bug report, **please include:**
- Your Host OS (Windows/Mac/Linux)
- The exact LLM model you were using (e.g., `openai/gpt-4o-mini`)
- The terminal output showing the crash

If you want to contribute code, we are actively looking for help battle-testing the GitHub App authentication path (Option B) across different repository permission structures!

## ⭐️ Show your support

If you find Ghost Hunter helpful, I would be incredibly grateful if you could give this repository a star! It helps others discover the project and keeps the project growing. 🌟

---

## 📜 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

<br>
<div align="center">
<i>Built with absolute precision.</i>
</div>
