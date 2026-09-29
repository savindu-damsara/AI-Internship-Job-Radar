
import os
import json
import re
import time
import hashlib
import smtplib
import argparse
import logging
from pathlib import Path
from email.message import EmailMessage
from html import unescape
from urllib.parse import urlsplit, urlunsplit

import requests
import feedparser
from dotenv import load_dotenv


# ---------------------------
# 1. BASIC CONFIGURATION
# ---------------------------

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "data" / "state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger("JobRadar")


# ---------------------------
# 2. LOAD SETTINGS
# ---------------------------

def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as file:
        return json.load(file)


def load_state():
    if not STATE_FILE.exists():
        return {
            "initialized": False,
            "seen": []
        }

    with open(STATE_FILE, encoding="utf-8") as file:
        return json.load(file)


def save_state(state):
    STATE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temporary = STATE_FILE.with_suffix(".tmp")

    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)

    temporary.replace(STATE_FILE)


# ---------------------------
# 3. FETCH RSS JOBS
# ---------------------------

def clean_text(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    return " ".join(unescape(value).split())


def normalize_url(url):
    parts = urlsplit(url.strip())

    # Remove tracking parameters and fragments.
    return urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        parts.path.rstrip("/"),
        "",
        ""
    ))


def make_job_id(job):
    raw = normalize_url(job["link"]) or (
        job["title"] + job["source"]
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


def fetch_feed(url):
    logger.info("Checking: %s", url)

    response = requests.get(
        url,
        timeout=25,
        headers={
            "User-Agent": "PersonalJobRadar/1.0"
        }
    )

    response.raise_for_status()

    parsed = feedparser.parse(response.content)

    if parsed.bozo and not parsed.entries:
        raise ValueError(
            f"Invalid or unreadable RSS feed: {url}"
        )

    jobs = []

    for entry in parsed.entries:
        link = entry.get("link", "").strip()

        if not link:
            continue

        job = {
            "title": clean_text(
                entry.get("title", "")
            ),
            "description": clean_text(
                entry.get("summary", "")
            ),
            "link": link,
            "published": entry.get(
                "published", "Not provided"
            ),
            "source": urlsplit(url).netloc
        }

        job["id"] = make_job_id(job)

        jobs.append(job)

    logger.info("Retrieved %s jobs", len(jobs))

    return jobs


# ---------------------------
# 4. FILTER JOBS
# ---------------------------

def matches_keywords(text, keywords):
    text = text.lower()

    return any(
        keyword.lower() in text
        for keyword in keywords
    )


def is_relevant(job, config):
    title = job["title"].lower()
    description = job["description"].lower()

    # The role must be relevant.
    if not matches_keywords(
        title + " " + description,
        config["job_keywords"]
    ):
        return False

    # Exclude senior positions by title.
    if matches_keywords(
        title,
        config["excluded_keywords"]
    ):
        return False

    # Require internship or entry-level wording.
    if config["require_entry_level"]:
        if not matches_keywords(
            title + " " + description,
            config["level_keywords"]
        ):
            return False

    return True


# ---------------------------
# 5. SEND GMAIL ALERT
# ---------------------------

def send_email(job):
    sender = os.getenv("GMAIL_ADDRESS")
    password = os.getenv("GMAIL_APP_PASSWORD")
    recipient = os.getenv("ALERT_EMAIL")

    if not all([sender, password, recipient]):
        raise ValueError(
            "Missing Gmail environment variables"
        )

    message = EmailMessage()

    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = (
        f"New Job Alert: {job['title']}"
    )

    message.set_content(
        f"NEW MATCHING JOB\n\n"
        f"Title: {job['title']}\n\n"
        f"Source: {job['source']}\n"
        f"Published: {job['published']}\n\n"
        f"Description:\n"
        f"{job['description'][:1000]}\n\n"
        f"Apply here:\n{job['link']}\n"
    )

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
        timeout=30
    ) as smtp:
        smtp.login(
            sender,
            password.replace(" ", "")
        )

        smtp.send_message(message)

    logger.info(
        "Email sent for: %s",
        job["title"]
    )


# ---------------------------
# 6. MAIN MONITORING LOGIC
# ---------------------------

def monitor(test=False):
    config = load_config()
    state = load_state()

    feeds = config.get("feeds", [])

    if not feeds:
        raise ValueError(
            "Add your RSS URLs to config.json first."
        )

    all_jobs = []
    successful_feeds = 0

    for url in feeds:
        try:
            jobs = fetch_feed(url)
            all_jobs.extend(jobs)
            successful_feeds += 1

            # Avoid rapid requests to different feeds.
            time.sleep(2)

        except Exception:
            logger.exception(
                "Failed to fetch feed: %s",
                url
            )

    if successful_feeds == 0:
        raise RuntimeError(
            "All RSS feeds failed. State unchanged."
        )

    # Deduplicate overlapping RSS feeds.
    unique_jobs = {
        job["id"]: job
        for job in all_jobs
    }

    relevant_jobs = [
        job for job in unique_jobs.values()
        if is_relevant(job, config)
    ]

    logger.info(
        "Relevant jobs found: %s",
        len(relevant_jobs)
    )

    # First run: remember existing jobs.
    # Don't send alerts for every old listing.
    if not state["initialized"] and not test:
        state["seen"] = list(unique_jobs.keys())
        state["initialized"] = True

        save_state(state)

        logger.info(
            "First run complete. Existing jobs saved."
        )

        return

    seen = set(state["seen"])

    new_jobs = [
        job for job in relevant_jobs
        if job["id"] not in seen
    ]

    logger.info(
        "New matching jobs: %s",
        len(new_jobs)
    )

    if test:
        for job in new_jobs[:5]:
            print(
                f"\n{job['title']}\n"
                f"{job['link']}\n"
            )
        return

    # Send and persist one job at a time.
    for job in new_jobs:
        try:
            send_email(job)

        except Exception:
            logger.exception(
                "Email failed for: %s",
                job["title"]
            )
            continue

        seen.add(job["id"])
        state["seen"] = sorted(seen)
        save_state(state)

    # Remember irrelevant jobs too.
    # Only do this after successful collection.
    for job in unique_jobs.values():
        if not is_relevant(job, config):
            seen.add(job["id"])

    state["seen"] = sorted(seen)
    save_state(state)

    logger.info("Monitoring completed.")


# ---------------------------
# 7. COMMAND LINE
# ---------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--test",
        action="store_true",
        help="Preview matching jobs without sending emails"
    )

    args = parser.parse_args()

    monitor(test=args.test)