"""Shared helper functions used by app.py, backtest.py, and fetch_prices.py."""

import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

# --- SMTP config ---
SMTP_HOST = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USER = os.environ.get('SMTP_USER', 'thebitcoinstrategy@gmail.com')
SMTP_PASS = os.environ.get('SMTP_PASS', 'gvcnyztughyyrlzp')
SMTP_FROM = os.environ.get('SMTP_FROM', 'Bitcoin Strategy <thebitcoinstrategy@gmail.com>')


def _build_email_msg(to_email, subject, html_body, attachments=None):
    """Build a MIME email message. attachments is a list of dicts:
    {'content': bytes, 'content_type': str, 'filename': str, 'content_id': str}"""
    if attachments:
        msg = MIMEMultipart('related')
        msg['Subject'] = subject
        msg['From'] = SMTP_FROM
        msg['To'] = to_email
        alt = MIMEMultipart('alternative')
        alt.attach(MIMEText(html_body, 'html'))
        msg.attach(alt)
        for att in attachments:
            img = MIMEImage(att['content'], name=att.get('filename', 'image.png'))
            img.add_header('Content-ID', f'<{att["content_id"]}>')
            img.add_header('Content-Disposition', 'inline', filename=att.get('filename', 'image.png'))
            msg.attach(img)
    else:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = SMTP_FROM
        msg['To'] = to_email
        msg.attach(MIMEText(html_body, 'html'))
    return msg


def send_email(to_email, subject, html_body, attachments=None):
    """Send a single email synchronously."""
    msg = _build_email_msg(to_email, subject, html_body, attachments)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)


def send_emails_batch(email_list, batch_size=50, batch_delay=2):
    """Send multiple emails over a single SMTP connection.
    email_list: list of dicts with keys: to, subject, html_body, attachments (optional).
    Returns (sent_count, failed_count)."""
    if not email_list:
        return 0, 0
    sent = 0
    failed = 0
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            for i, item in enumerate(email_list):
                try:
                    msg = _build_email_msg(
                        item['to'], item['subject'], item['html_body'],
                        item.get('attachments')
                    )
                    server.send_message(msg)
                    sent += 1
                except Exception as e:
                    print(f"[EMAIL ERROR] Failed to send to {item.get('to')}: {e}")
                    failed += 1
                # Pause between batches to avoid spam flags
                if (i + 1) % batch_size == 0 and (i + 1) < len(email_list):
                    time.sleep(batch_delay)
    except Exception as e:
        print(f"[EMAIL ERROR] SMTP connection failed: {e}")
        failed += len(email_list) - sent
    return sent, failed


def compute_ratio_prices(df, df_vs):
    """Divide df's close by df_vs's close on common dates.

    Both DataFrames must have a DatetimeIndex and a 'close' column.
    Returns a modified copy of df with only the overlapping dates,
    where close = df.close / df_vs.close.

    Raises ValueError if there are no overlapping dates.
    """
    df = df.copy()
    df_vs = df_vs.copy()
    df.index = df.index.normalize()
    df_vs.index = df_vs.index.normalize()
    df = df[~df.index.duplicated(keep='first')]
    df_vs = df_vs[~df_vs.index.duplicated(keep='first')]
    common = df.index.intersection(df_vs.index)
    if len(common) == 0:
        raise ValueError("No overlapping dates between the two assets")
    df = df.loc[common]
    df["close"] = df["close"] / df_vs.loc[common, "close"]
    return df
