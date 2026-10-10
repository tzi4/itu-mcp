"""Experimental ITU YETKIM login using host-supplied credentials in memory."""
from __future__ import annotations

from urllib.parse import urlencode, urlsplit

from .browser import RetrievalUnavailable
from .ieee import ieee_url, article_request_allowed


IDP_HOST = "yetkim.itu.edu.tr"
ENTITY_ID = "https://yetkim.itu.edu.tr/idp/shibboleth"
SSO_PATH = "/idp/profile/SAML2/Redirect/SSO"


def trusted_idp_url(url: str, *, login_form: bool = False) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname == IDP_HOST
                and parsed.port in (None, 443) and not parsed.username and not parsed.password
                and not any(c in url for c in ("\\", "\r", "\n", "\t"))
                and (not login_form or parsed.path == SSO_PATH))
    except ValueError:
        return False


def ieee_login_url(article: str) -> str:
    if not ieee_url(article):
        raise ValueError("An IEEE HTTPS return URL is required.")
    # Standard publisher-initiated SAML entry point. ITU's entity ID is taken
    # from its public metadata, never from an MCP argument or a stale execution ID.
    return "https://ieeexplore.ieee.org/servlet/wayf.jsp?" + urlencode({
        "entityId": ENTITY_ID, "url": article})


def itu_username(value: str) -> str:
    """Map the host's ITU mail address to the short YETKIM account name."""
    if not isinstance(value, str):
        raise RetrievalUnavailable("institution_username_format_unsupported")
    value = value.strip()
    if "@" in value:
        local, domain = value.rsplit("@", 1)
        if domain.casefold() != "itu.edu.tr" or "@" in local:
            raise RetrievalUnavailable("institution_username_format_unsupported")
        value = local
    if not value or any(c.isspace() for c in value):
        raise RetrievalUnavailable("institution_username_format_unsupported")
    return value


def fill_itu_form(page, credential_source):
    """Only obtain the host's secret after validating the page and form target."""
    if not trusted_idp_url(page.url, login_form=True):
        raise RetrievalUnavailable("institution_login_destination_rejected")
    password = page.locator('input[type="password"]:visible')
    if password.count() != 1:
        raise RetrievalUnavailable("institution_login_form_unrecognized")
    destination = password.evaluate("el => el.form ? {action: el.form.action, method: el.form.method} : null")
    if (not destination or destination["method"].lower() != "post"
            or not trusted_idp_url(destination["action"], login_form=True)):
        raise RetrievalUnavailable("institution_login_destination_rejected")
    form = password.locator("xpath=ancestor::form")
    username = form.locator('input[name="j_username"]:visible')
    if username.count() != 1:
        username = form.locator('input[type="text"]:visible')
    submit = form.locator('[name="_eventId_proceed"]')
    if submit.count() != 1:
        submit = form.locator('button[type="submit"], input[type="submit"], button:not([type])')
    if username.count() != 1 or submit.count() != 1:
        raise RetrievalUnavailable("institution_login_form_unrecognized")
    try:
        credentials = credential_source()
        if not credentials.username or not credentials.password:
            raise ValueError()
    except Exception:
        raise RetrievalUnavailable("institution_credentials_unavailable") from None
    try:
        username.fill(itu_username(credentials.username))
        password.fill(credentials.password)
        submit.click(timeout=10000)
    finally:
        credentials = None


def authenticate_ieee(session, article: str, credential_source):
    """Submit at most once. A returned IEEE session still needs a PDF access test."""
    from playwright.sync_api import Error

    page = session.new_page()
    page.route("**/*", lambda route: route.fallback() if (
        article_request_allowed(route.request) or trusted_idp_url(route.request.url)) else route.abort())
    try:
        response = page.goto(ieee_login_url(article), timeout=30000, wait_until="domcontentloaded")
        if not response or response.status != 200:
            raise RetrievalUnavailable("institution_login_entry_unavailable")
        if trusted_idp_url(page.url, login_form=True):
            page.locator('input[type="password"]:visible').wait_for(state="visible", timeout=10000)
            fill_itu_form(page, credential_source)
        page.wait_for_url(lambda url: ieee_url(str(url)) and "/document/" in str(url), timeout=30000)
    except Error:
        # Never include a filled form, request body or Playwright call log.
        raise RetrievalUnavailable("institution_login_interaction_required") from None
    finally:
        page.close()
