"""
Gmail Service
Handles OAuth2 authentication and fetching/parsing newsletter emails.
"""

import base64
import re
from datetime import datetime, timedelta
from typing import Optional
from email import message_from_bytes
from bs4 import BeautifulSoup

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Common newsletter sender domains to prioritise
NEWSLETTER_KEYWORDS = [
    "newsletter", "digest", "briefing", "substack",
    "axios", "morning brew", "noreply", "no-reply",
    "hello@", "hi@", "news@", "daily@", "weekly@",
]


class GmailService:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.last_refreshed_token: Optional[dict] = None

    def get_auth_url(self) -> str:
        flow = self._make_flow()
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        return auth_url

    def exchange_code(self, code: str) -> dict:
        flow = self._make_flow()
        flow.fetch_token(code=code)
        creds = flow.credentials
        return self._creds_to_dict(creds)

    def fetch_newsletters(
        self,
        token: dict,
        hours: int = 24,
        max_results: int = 30,
    ) -> list[dict]:
        """
        Fetch all emails from the dedicated newsletter Gmail account.
        No filtering needed — everything in this inbox is a newsletter.
        """
        creds = self._dict_to_creds(token)

        # Refresh token if expired
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            self.last_refreshed_token = self._creds_to_dict(creds)

        service = build("gmail", "v1", credentials=creds)

        since = (datetime.utcnow() - timedelta(hours=hours)).strftime("%Y/%m/%d")

        # Dedicated newsletter account — just fetch everything in inbox, no filtering
        query = f"in:inbox after:{since}"

        results = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=max_results,
        ).execute()

        messages = results.get("messages", [])
        articles = []

        for msg_ref in messages:
            try:
                msg = service.users().messages().get(
                    userId="me",
                    id=msg_ref["id"],
                    format="full",
                ).execute()

                article = self._parse_message(msg)
                if article:
                    articles.append(article)
            except Exception as e:
                print(f"[Gmail] Error parsing message {msg_ref['id']}: {e}")
                continue

        return articles

    def _parse_message(self, msg: dict) -> Optional[dict]:
        """Extract structured data from a Gmail message."""
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}

        subject = headers.get("Subject", "No subject")
        sender = headers.get("From", "")
        date_str = headers.get("Date", "")
        msg_id = msg["id"]

        # Parse sender name and email
        sender_name, sender_email = self._parse_sender(sender)

        # Extract body
        body_text, body_html = self._extract_body(msg["payload"])

        # Use HTML for better content extraction if available
        content = ""
        if body_html:
            content = self._html_to_text(body_html)
        elif body_text:
            content = body_text

        if not content or len(content) < 50:
            return None

        # Truncate for storage
        content = content[:3000]
        excerpt = content[:200].strip()

        return {
            "id": msg_id,
            "headline": subject,
            "publication": sender_name,
            "sender_email": sender_email,
            "excerpt": excerpt,
            "body": content,
            "time": self._format_time(date_str),
            "source": self._infer_source(sender_email, sender_name),
            "color": self._source_color(sender_email),
            "size": "medium",
            "category": self._infer_category(subject, content),
            "readTime": f"{max(1, len(content.split()) // 200)} min",
            "bgGradient": "linear-gradient(135deg, #0f0f0f 0%, #1a1a1a 100%)",
        }

    def _is_newsletter(self, article: dict) -> bool:
        """Filter out transactional emails, keep newsletters."""
        sender = article.get("sender_email", "").lower()
        headline = article.get("headline", "").lower()

        # Skip obvious non-newsletters
        skip_patterns = [
            "receipt", "order", "invoice", "payment", "verification",
            "password", "login", "security", "alert", "otp", "code",
            "delivery", "tracking", "shipped", "refund",
        ]
        for p in skip_patterns:
            if p in headline or p in sender:
                return False

        # Must have meaningful content
        if len(article.get("body", "")) < 100:
            return False

        return True

    def _extract_body(self, payload: dict) -> tuple[str, str]:
        """Recursively extract plain text and HTML from email payload."""
        text_plain = ""
        text_html = ""

        mime_type = payload.get("mimeType", "")

        if mime_type == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                text_plain = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

        elif mime_type == "text/html":
            data = payload.get("body", {}).get("data", "")
            if data:
                text_html = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

        elif "parts" in payload:
            for part in payload["parts"]:
                p, h = self._extract_body(part)
                text_plain = text_plain or p
                text_html = text_html or h

        return text_plain, text_html

    def _html_to_text(self, html: str) -> str:
        """Convert HTML email to clean readable text."""
        soup = BeautifulSoup(html, "html.parser")

        # Remove boilerplate elements
        for tag in soup(["script", "style", "nav", "footer", "header", "img",
                         "button", "input", "form", "iframe", "noscript"]):
            tag.decompose()

        # Remove unsubscribe sections (common footer text)
        for tag in soup.find_all(string=re.compile(r"unsubscribe|opt.out|manage.preferences", re.I)):
            if tag.parent:
                tag.parent.decompose()

        text = soup.get_text(separator="\n")

        # Clean up whitespace
        lines = [line.strip() for line in text.splitlines()]
        lines = [l for l in lines if l and len(l) > 2]
        text = "\n".join(lines)

        # Collapse multiple newlines
        text = re.sub(r"\n{3,}", "\n\n", text)

        return text.strip()

    def _parse_sender(self, sender: str) -> tuple[str, str]:
        """Parse 'Display Name <email@domain.com>' format."""
        match = re.match(r'^"?([^"<]+)"?\s*<([^>]+)>', sender)
        if match:
            return match.group(1).strip(), match.group(2).strip().lower()
        if "@" in sender:
            return sender.split("@")[0].title(), sender.strip().lower()
        return sender, ""

    def _infer_source(self, email: str, name: str) -> str:
        """Map sender to a known source ID."""
        combined = (email + name).lower()
        mapping = {
            "axios": "axios",
            "ft.com": "ft",
            "economist": "economist",
            "substack": "substack",
            "nzherald": "nzherald",
            "techcrunch": "techcrunch",
            "morningbrew": "morningbrew",
            "theinformation": "theinformation",
        }
        for key, source_id in mapping.items():
            if key in combined:
                return source_id
        return "newsletter"

    def _source_color(self, email: str) -> str:
        colors = {
            "axios": "#FF4D4D", "ft.com": "#F9C784",
            "economist": "#E3120B", "substack": "#FF6719",
            "nzherald": "#4D9FFF", "techcrunch": "#4DCC4D",
        }
        for domain, color in colors.items():
            if domain in email:
                return color
        return "#888888"

    def _infer_category(self, subject: str, content: str) -> str:
        combined = (subject + content[:200]).lower()
        categories = {
            "Markets": ["market", "stocks", "fed", "interest rate", "inflation", "economy"],
            "AI & Tech": ["ai", "artificial intelligence", "gpt", "llm", "machine learning", "tech"],
            "Venture": ["startup", "vc", "venture", "funding", "seed", "series"],
            "New Zealand": ["nz", "new zealand", "auckland", "wellington", "govt"],
            "Global": ["global", "trade", "geopolitic", "international"],
            "Policy": ["policy", "regulation", "senate", "congress", "government"],
        }
        for cat, keywords in categories.items():
            if any(k in combined for k in keywords):
                return cat
        return "News"

    def _format_time(self, date_str: str) -> str:
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(date_str)
            diff = datetime.now(dt.tzinfo) - dt
            hours = int(diff.total_seconds() / 3600)
            if hours < 1:
                return "Just now"
            if hours < 24:
                return f"{hours}h ago"
            return f"{int(hours/24)}d ago"
        except Exception:
            return "Today"

    def _make_flow(self) -> Flow:
        client_config = {
            "web": {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uris": [self.redirect_uri],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }
        flow = Flow.from_client_config(client_config, scopes=SCOPES)
        flow.redirect_uri = self.redirect_uri
        return flow

    def _creds_to_dict(self, creds: Credentials) -> dict:
        return {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes or []),
        }

    def _dict_to_creds(self, token: dict) -> Credentials:
        return Credentials(
            token=token.get("token"),
            refresh_token=token.get("refresh_token"),
            token_uri=token.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=token.get("client_id", self.client_id),
            client_secret=token.get("client_secret", self.client_secret),
            scopes=token.get("scopes", SCOPES),
        )
