"""Publisher identification is discovery metadata, never retrieval authorization."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Platform:
    name: str
    title: str
    hosts: frozenset[str]
    publisher_names: tuple[str, ...]
    implemented: bool = False


PLATFORMS = (
    Platform('mdpi', 'MDPI', frozenset({'www.mdpi.com', 'mdpi-res.com'}),
             ('mdpi ag', 'mdpi')),
    Platform('ieee', 'IEEE Xplore', frozenset({'ieeexplore.ieee.org'}),
             ('ieee', 'institute of electrical and electronics engineers',
              'institute of electrical and electronics engineers (ieee)'), True),
    Platform('springer', 'Springer Nature Link', frozenset({'link.springer.com', 'link.springernature.com'}),
             ('springer', 'springer science and business media llc', 'springer nature'), True),
    Platform('elsevier', 'ScienceDirect', frozenset({'www.sciencedirect.com', 'linkinghub.elsevier.com'}), ('elsevier', 'elsevier bv', 'elsevier ltd')),
    Platform('wiley', 'Wiley Online Library', frozenset({'onlinelibrary.wiley.com'}), ('wiley', 'john wiley & sons', 'john wiley & sons, ltd')),
    Platform('nature', 'Nature Portfolio', frozenset({'www.nature.com', 'nature.com'}), ('nature publishing group',)),
    Platform('cambridge', 'Cambridge Core', frozenset({'www.cambridge.org', 'resolve.cambridge.org'}), ('cambridge university press', 'cambridge university press (cup)'), True),
    Platform('oxford', 'Oxford Academic', frozenset({'academic.oup.com'}), ('oxford university press', 'oxford university press (oup)'), True),
    Platform('aiaa', 'AIAA Aerospace Research Central', frozenset({'arc.aiaa.org'}), ('american institute of aeronautics and astronautics (aiaa)',)),
    Platform('acm', 'ACM Digital Library', frozenset({'dl.acm.org'}), ('association for computing machinery (acm)', 'association for computing machinery'), True),
    Platform('acs', 'ACS Publications', frozenset({'pubs.acs.org'}), ('american chemical society (acs)', 'american chemical society'), True),
    Platform('sage', 'SAGE Journals', frozenset({'journals.sagepub.com'}), ('sage publications', 'sage publications ltd', 'sage publications inc'), True),
    Platform('taylor_francis', 'Taylor & Francis', frozenset({'www.tandfonline.com', 'www.taylorfrancis.com'}), ('informa uk limited', 'taylor & francis'), True),
    Platform('asme', 'ASME Digital Collection', frozenset({'asmedigitalcollection.asme.org'}), ('asme international', 'american society of mechanical engineers'), True),
    Platform('iop', 'IOPscience', frozenset({'iopscience.iop.org'}), ('iop publishing',)),
    Platform('rsc', 'RSC Publishing', frozenset({'pubs.rsc.org'}), ('royal society of chemistry (rsc)', 'royal society of chemistry')),
    Platform('emerald', 'Emerald Insight', frozenset({'www.emerald.com', 'www.emeraldinsight.com'}), ('emerald', 'emerald publishing limited')),
)
BY_NAME = {p.name: p for p in PLATFORMS}


def identify_platform(paper: dict) -> str:
    """Use exact HTTPS hosts first, then conservative publisher-name aliases.

    A publisher family can contain separate platforms with different login flows.
    For example a nature.com source must not select the Springer Link adapter.
    Providers still validate their own article path and DOI before any retrieval.
    """
    raw = paper.get('publisher_url')
    if isinstance(raw, str):
        try:
            p = urlsplit(raw)
            if (p.scheme == 'https' and p.port in (None, 443) and not p.username and not p.password
                    and '\\' not in raw and not any(ord(c) < 33 for c in raw)):
                for platform in PLATFORMS:
                    if p.hostname in platform.hosts:
                        return platform.name
        except ValueError:
            pass
    publisher = paper.get('publisher')
    if isinstance(publisher, str):
        publisher = ' '.join(publisher.casefold().split()).rstrip('.')
        for platform in PLATFORMS:
            if publisher in platform.publisher_names:
                return platform.name
    return 'unknown'


def platform_coverage(configured: list[str]) -> list[dict]:
    return [{'provider': p.name, 'platform': p.title,
             'adapter': ('configured_experimental' if p.name in configured else 'not_configured')
                        if p.implemented else 'not_implemented',
             'entitlement': 'not_checked'} for p in PLATFORMS]


def unavailable_next_action(reason: str) -> str:
    if reason in {'publisher_not_supported', 'publisher_retrieval_not_implemented', 'browser_session_not_configured'}:
        return ('Use papers_resolve for access links or select another relevant source with retrievable text. '
                'Do not claim this paper was read and do not repeatedly retry an unimplemented adapter.')
    if reason in {'metadata_lookup_failed', 'metadata_identity_not_verified'}:
        return ('Verify the reference with discovery or select another source. No publisher text was read.')
    if reason in {'browser_busy', 'browser_operation_timeout'}:
        return ('A browser operation is still pending or timed out. Continue with cached sources; '
                'do not start repeated acquisition or login calls.')
    if 'permission' in reason or 'credentials' in reason:
        return ('This source requires operator access configuration. Continue with other authorized sources '
                'and disclose the missing evidence; metadata is not full text.')
    return ('The publisher access attempt did not produce verified text. Continue with other sources '
            'and disclose the missing evidence. Do not retry a challenge or login in a loop.')
