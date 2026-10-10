"""ITU YETKIM login through Springer's observed institutional SAML entry."""
from __future__ import annotations

from urllib.parse import urlencode
from .browser import RetrievalUnavailable
from .itu_login import ENTITY_ID, trusted_idp_url, fill_itu_form
from .springer import springer_url


def springer_login_url(article: str) -> str:
    if not springer_url(article):
        raise ValueError('A Springer Nature Link HTTPS return URL is required.')
    return 'https://sp.springer.com/saml/login?' + urlencode({'idp': ENTITY_ID, 'targetUrl': article})


def authenticate_springer(session, article, credential_source):
    from playwright.sync_api import Error
    page = session.new_page()
    page.route('**/*', lambda route: route.fallback() if (
        springer_url(route.request.url, login=True) or trusted_idp_url(route.request.url)) else route.abort())
    try:
        page.goto(springer_login_url(article), wait_until='domcontentloaded', timeout=30000)
        page.wait_for_url(lambda url: trusted_idp_url(str(url), login_form=True)
                          or springer_url(str(url)), timeout=30000)
        if trusted_idp_url(page.url, login_form=True):
            page.locator('input[type="password"]:visible').wait_for(timeout=10000)
            fill_itu_form(page, credential_source)
        page.wait_for_url(lambda url: springer_url(str(url)), timeout=30000)
    except Error:
        raise RetrievalUnavailable('institution_login_interaction_required') from None
    finally:
        page.close()
