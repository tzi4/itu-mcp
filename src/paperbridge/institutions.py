"""Institution-specific configuration, separate from discovery and document storage."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, urlencode


@dataclass(frozen=True)
class ItuProfile:
    def authenticate_springer(self, session, article_url, credentials):
        from .springer_login import authenticate_springer
        return authenticate_springer(session, article_url, credentials)

    def authenticate_ieee(self, session, article_url, credentials):
        from .itu_login import authenticate_ieee
        return authenticate_ieee(session, article_url, credentials)

    def entry_points(self, provider: str) -> dict:
        # Verified platform links, not a guarantee of article-level deep linking.
        landing = {"elsevier": "https://www.sciencedirect.com/",
                   "springer": "https://link.springer.com/",
                   "ieee": "https://www.ieee.org/",
                   "wiley": "https://onlinelibrary.wiley.com/",
                   "aiaa": "https://arc.aiaa.org/",
                   "acm": "https://dl.acm.org/",
                   "acs": "https://pubs.acs.org/",
                   "cambridge": "https://www.cambridge.org/core",
                   "oxford": "https://academic.oup.com/journals",
                   "sage": "https://journals.sagepub.com/",
                   "taylor_francis": "https://www.tandfonline.com/",
                   "asme": "https://asmedigitalcollection.asme.org/"}.get(provider)
        platform_login = (("https://go.openathens.net/redirector/itu.edu.tr?url="
                           + quote(landing, safe="")) if landing else None)
        if provider in {"wiley", "aiaa", "sage", "taylor_francis"}:
            platform_login = landing.rstrip('/') + '/action/ssostart?' + urlencode({
                'idp': 'https://idp.eduserv.org.uk/openathens'})
        if provider == "ieee":
            from .itu_login import ieee_login_url
            platform_login = ieee_login_url("https://ieeexplore.ieee.org/Xplore/home.jsp")
        elif provider == "springer":
            from .springer_login import springer_login_url
            platform_login = springer_login_url("https://link.springer.com/")
        from .openathens import PUBLISHERS, openathens_configured
        openathens = provider in PUBLISHERS
        return {"name": "Istanbul Technical University",
                "platform_login_url": platform_login,
                "authentication": "itu_yetkim_saml" if provider in {"ieee", "springer"}
                    else "itu_openathens" if openathens else "openathens_or_provider",
                "directory_url": "https://service.library.itu.edu.tr/web/Veritabanlari/Tumu",
                "entitlement": "not_checked",
                "login_automated": provider in {"ieee", "springer"} or (openathens and openathens_configured()),
                "credential_kind": "itu_institutional_account" if provider in {"ieee", "springer"}
                    else "itu_library_account" if landing else "not_resolved"}
