from __future__ import annotations

import json
import re
from http.cookiejar import CookieJar
from typing import Any, Optional, Union
from urllib.parse import urlparse

import click
from click import Context

from envied.core.credential import Credential
from envied.core.manifests.dash import DASH
from envied.core.service import Service
from envied.core.session import session as create_curl_session
from envied.core.titles import Movie, Movies
from envied.core.tracks import Chapters, Tracks


SLUG_RE = re.compile(r"thunderflix\.com/videos/(?P<slug>[a-z0-9\-]+)", re.IGNORECASE)
# Match the main embed URL — must include is_trailer=false so we skip trailer iframes.
EMBED_URL_RE = re.compile(
    r"https://embed\.vhx\.tv/videos/(?P<video_id>\d+)\?(?P<query>[^\"'\s<>]*?is_trailer=false[^\"'\s<>]*)",
)
VIMEO_CONFIG_RE = re.compile(
    r"https://player\.vimeo\.com/video/(?P<clip_id>\d+)/config\?[^\"'\s<>]+",
)


class THFX(Service):
    """
    Service code for the Thunderflix streaming service (https://www.thunderflix.com/).

    \b
    Authorization: Cookies (Cookies/THFX.txt — Mozilla/Netscape format)
    Security: Widevine L3
    Catalogue: Movies, documentaries, concerts (no series numbering)

    \b
    Usage:
        Export your logged-in browser cookies for thunderflix.com to
        Cookies/THFX.txt, then pass the full video URL:

        uv run envied dl THFX https://www.thunderflix.com/videos/<slug>
    """

    ALIASES = ("Thunderflix", "thunderflix")

    @staticmethod
    def get_session():
        """TLS-fingerprinted session — Vimeo/Cloudflare gates the license endpoint
        on JA3, so plain python-requests is rejected."""
        return create_curl_session("Chrome131", max_retries=3, status_forcelist=[429, 500, 502, 503, 504])

    @staticmethod
    @click.command(name="THFX", short_help="https://www.thunderflix.com/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> "THFX":
        return THFX(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        self.title = title
        super().__init__(ctx)
        self.session.headers.update(self.config["headers"])

        match = SLUG_RE.search(title)
        if not match:
            raise ValueError(
                f"Could not extract slug from {title!r}. "
                f"Expected a URL like https://www.thunderflix.com/videos/<slug>"
            )
        self.slug = match.group("slug")

        self.video_id: Optional[str] = None
        self.auth_user_token: Optional[str] = None
        self.embed_url: Optional[str] = None
        self.license_url: Optional[str] = None
        self.widevine_certificate: Optional[str] = None
        self.atid: Optional[str] = None

    def authenticate(
        self,
        cookies: Optional[CookieJar] = None,
        credential: Optional[Credential] = None,
    ) -> None:
        super().authenticate(cookies, credential)
        if not cookies:
            raise EnvironmentError(
                "Service requires cookies — export your browser session for "
                "thunderflix.com to Cookies/THFX.txt"
            )

    def get_titles(self) -> Movies:
        page = self._fetch_page()
        title_name = self._extract_meta(page, "og:title") or self.slug
        description = self._extract_meta(page, "og:description") or ""
        year = self._extract_year(page)

        return Movies([
            Movie(
                id_=self.video_id,
                service=self.__class__,
                name=title_name,
                year=year,
                language="en",
                data={"slug": self.slug},
            )
        ])

    def get_tracks(self, title: Movie) -> Tracks:
        if not self.embed_url:
            self._fetch_page()
        r = self.session.get(
            self.embed_url,
            headers={"Referer": "https://www.thunderflix.com/"},
        )
        if r.status_code != 200:
            raise ConnectionError(
                f"Failed to fetch embed page ({r.status_code}): url={self.embed_url!r} body={r.text[:300]!r}"
            )

        embed_body = self._decode_escapes(r.text)
        config_match = VIMEO_CONFIG_RE.search(embed_body)
        if not config_match:
            # Some VHX embeds put the config URL in the iframe ?config= param,
            # in window.playerConfig, or as data-config-url attributes.
            config_match = re.search(
                r"https://player\.vimeo\.com/video/(?P<clip_id>\d+)/config[^\"'\s<>]*",
                embed_body,
            )
        if not config_match:
            raise ValueError(
                "Could not find Vimeo config URL in embed page."
            )
        config_url = config_match.group(0).replace("&amp;", "&")
        clip_id = config_match.group("clip_id")

        config_r = self.session.get(
            config_url,
            headers={
                "Referer": "https://embed.vhx.tv/",
                "Origin": "https://embed.vhx.tv",
            },
        )
        if config_r.status_code != 200:
            raise ConnectionError(
                f"Failed to fetch Vimeo player config ({config_r.status_code}): "
                f"url={config_url!r} body={config_r.text[:300]!r}"
            )
        try:
            vimeo_config = config_r.json()
        except json.JSONDecodeError:
            raise ValueError(f"Vimeo config did not return JSON: {config_r.text[:200]}")

        mpd_url, license_url = self._extract_dash_urls(vimeo_config, clip_id)
        if not mpd_url:
            raise ValueError("No DASH manifest URL found in Vimeo config.")
        if not license_url:
            raise ValueError(
                "Could not find a signed Widevine license URL in Vimeo config "
                "(request.drm.cdms.widevine.license_url)."
            )
        self.license_url = license_url
        self.atid = vimeo_config.get("request", {}).get("atid")
        widevine = (
            vimeo_config.get("request", {}).get("drm", {}).get("cdms", {}).get("widevine", {})
        )
        self.widevine_certificate = widevine.get("certificate")

        jar = self.session.cookies
        cookie_names = list(jar.keys()) if hasattr(jar, "keys") else [
            getattr(c, "name", str(c)) for c in jar
        ]
        if "vuid" not in cookie_names:
            self.log.warning(
                "No vuid cookie found — Vimeo will likely reject the license call. "
                "Export your vimeo.com cookies into Cookies/THFX.txt."
            )

        tracks = DASH.from_url(url=mpd_url, session=self.session).to_tracks(
            language=title.language
        )

        # Default cap: 1080p. UHD content costs disk + license-server time and
        # is rarely worth it for this catalogue. Users can override with
        # envied's --quality flag at the CLI.
        max_height = 1080
        before = len(tracks.videos)
        tracks.videos = [v for v in tracks.videos if (v.height or 0) <= max_height]
        if len(tracks.videos) < before:
            self.log.info(
                f"Capped video tracks at {max_height}p "
                f"({before - len(tracks.videos)} higher-resolution variants dropped)"
            )

        return tracks

    def get_chapters(self, title: Movie) -> Chapters:
        return Chapters()

    def get_widevine_service_certificate(self, **_: Any) -> Optional[str]:
        return self.widevine_certificate

    def get_widevine_license(self, *, challenge: bytes, **_: Any) -> Optional[bytes]:
        if not self.license_url:
            raise RuntimeError("License URL not set — get_tracks must run first.")

        runtime_params = (
            f"&atid={self.atid}"
            "&referrer=https%3A%2F%2Fwww.thunderflix.com%2Fbrowse"
            "&first_log=1"
            "&player_location=onsite"
            "&playback_route=player_embed_ott"
        )
        url = self.license_url + runtime_params

        # Step 1: GET the Vimeo license endpoint. It's not the actual Widevine
        # license server — it returns a redirect/token to ExpressPlay, which is
        # the real Widevine proxy that processes the challenge.
        jar = self.session.cookies
        if hasattr(jar, "get_dict"):
            vimeo_cookies = {**jar.get_dict(domain=".vimeo.com"),
                             **jar.get_dict(domain="vimeo.com")}
        else:
            vimeo_cookies = {}
        cookie_header = "; ".join(f"{k}={v}" for k, v in vimeo_cookies.items())

        token_resp = self.session.get(
            url=url,
            headers={
                "Accept": "*/*",
                "Origin": "https://embed.vhx.tv",
                "Referer": "https://embed.vhx.tv/",
                "Cookie": cookie_header,
            },
        )
        if token_resp.status_code != 200:
            self.log.error(f"Vimeo token-exchange response headers: {dict(token_resp.headers)}")
            self.log.error(f"Body: {token_resp.text[:500]}")
            raise ConnectionError(
                f"Vimeo token-exchange failed ({token_resp.status_code})"
            )

        # The response body contains the ExpressPlay URL.
        body = token_resp.text
        m = re.search(r"https://wv\.service\.expressplay\.com/[^\"'\s<>]+", body)
        if not m:
            self.log.error(f"Token response body: {body[:500]}")
            raise ValueError(
                "Could not find ExpressPlay URL in Vimeo token-exchange response."
            )
        expressplay_url = m.group(0)
        self.log.debug(f"ExpressPlay URL: {expressplay_url}")

        # Step 2: POST the Widevine challenge to ExpressPlay.
        r = self.session.post(
            url=expressplay_url,
            data=challenge,
            headers={
                "Accept": "*/*",
                "Content-Type": "application/octet-stream",
                "Origin": "https://embed.vhx.tv",
                "Referer": "https://embed.vhx.tv/",
            },
        )
        if r.status_code != 200:
            self.log.error(f"ExpressPlay response headers: {dict(r.headers)}")
            self.log.error(f"ExpressPlay body: {r.text[:500]}")
            raise ConnectionError(
                f"ExpressPlay license request failed ({r.status_code})"
            )
        return r.content

    def _fetch_page(self) -> str:
        page_url = self.config["endpoints"]["page"].format(slug=self.slug)
        # rnet's cookie jar may URL-decode or otherwise mangle the _session
        # cookie value. Build the Cookie header manually from raw values.
        try:
            jar = self.session.cookies
            raw_cookies = (
                jar.get_dict(domain=".thunderflix.com")
                if hasattr(jar, "get_dict")
                else dict(jar)
            )
            # Add www-domain cookies too (different cookie jar bucket).
            if hasattr(jar, "get_dict"):
                raw_cookies.update(jar.get_dict(domain=".www.thunderflix.com"))
                raw_cookies.update(jar.get_dict(domain="www.thunderflix.com"))
            cookie_header = "; ".join(f"{k}={v}" for k, v in raw_cookies.items())
            self.log.info(f"Sending Cookie header with {len(raw_cookies)} entries; _session={'_session' in raw_cookies}")
        except Exception as e:
            self.log.warning(f"Could not build Cookie header: {e}")
            cookie_header = ""
        r = self.session.get(
            page_url,
            headers={"Cookie": cookie_header} if cookie_header else None,
        )
        if r.status_code != 200:
            raise ConnectionError(
                f"Failed to fetch video page ({r.status_code}). "
                f"Cookies may be expired."
            )
        page = self._decode_escapes(r.text)
        # Confirm whether the page treats us as logged-in.
        if '"id":null' in page and 'window._current_user' in page:
            self.log.error(
                "Page rendered as anonymous (window._current_user.id is null). "
                "Cookies are not being recognized — check _session value or expiry."
            )
        self._cached_page = page

        embed_match = EMBED_URL_RE.search(page)
        if not embed_match:
            # Dump every embed.vhx occurrence so we can spot the real one.
            for i, m in enumerate(re.finditer(r"embed\.vhx[^\"'\s<>]+", page)):
                self.log.error(f"embed.vhx hit {i}: {m.group(0)[:300]!r}")
            self.log.error(
                f"Contains auth-user-token: {'auth-user-token' in page}"
            )
            self.log.error(
                f"Contains main video_id 3465855: {'3465855' in page}"
            )
            raise ValueError(
                "Could not find embed iframe in page — cookies may be expired "
                "or the page layout has changed."
            )
        self.video_id = embed_match.group("video_id")
        self.auth_user_token = self._mint_auth_user_token(self.video_id)
        # Strip any pre-existing auth-user-token from the page-rendered query
        # before we append our own — the page sometimes includes a placeholder.
        clean_query = re.sub(
            r"&?auth-user-token=[^&]*", "", embed_match.group("query")
        ).lstrip("&")
        self.embed_url = (
            f"https://embed.vhx.tv/videos/{self.video_id}?"
            f"{clean_query}&auth-user-token={self.auth_user_token}"
        )
        return page

    def _mint_auth_user_token(self, video_id: str) -> str:
        """Get an auth-user-token from VHX. The token is rendered into the page
        HTML by the server using the _session cookie. The browser's JS reads
        it and constructs the iframe URL on the fly."""
        # First try: scan the page we already have for any JWT (eyJ...).
        page = getattr(self, "_cached_page", None)
        if page is None:
            page = self._fetch_raw_page()
        # auth-user-token is HS256 with payload {user_id, exp} only (2 keys).
        # Reject the API client token (RS256, has app_id/site_id/scopes etc).
        HS256_HEADER = "eyJhbGciOiJIUzI1NiJ9"
        jwt_re = re.compile(rf"{HS256_HEADER}\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
        candidates = jwt_re.findall(page)
        import base64
        for jwt in candidates:
            parts = jwt.split(".")
            try:
                pad = "=" * (-len(parts[1]) % 4)
                payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
                if set(payload.keys()) == {"user_id", "exp"}:
                    self.log.info(
                        f"Found auth-user-token in page (user_id={payload['user_id']})"
                    )
                    return jwt
            except Exception:
                continue
        # If we didn't find one, dump the page so we can iterate.
        try:
            from envied.core.config import config as envied_config
            dump_path = str(envied_config.directories.temp / "thfx_page.html")
            with open(dump_path, "w") as f:
                f.write(page)
            self.log.error(f"Dumped page to {dump_path}")
        except Exception:
            pass
        self.log.error(f"Found {len(candidates)} JWT-shaped strings, none with user_id+exp payload")
        raise ValueError(
            "Could not find auth-user-token in page. Inspect the dumped HTML."
        )

    def _fetch_raw_page(self) -> str:
        page_url = self.config["endpoints"]["page"].format(slug=self.slug)
        r = self.session.get(page_url)
        return self._decode_escapes(r.text)

    @staticmethod
    def _decode_escapes(text: str) -> str:
        return (
            text.replace("\\u0026", "&")
                .replace("\\u003d", "=")
                .replace("\\/", "/")
                .replace("&amp;", "&")
        )

    @staticmethod
    def _extract_meta(page: str, prop: str) -> Optional[str]:
        match = re.search(
            rf'<meta[^>]+property=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)["\']',
            page,
        )
        if match:
            return match.group(1)
        match = re.search(
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(prop)}["\']',
            page,
        )
        return match.group(1) if match else None

    @staticmethod
    def _extract_year(page: str) -> Optional[int]:
        match = re.search(r'"release_year"\s*:\s*(\d{4})', page) or re.search(
            r'datetime=["\'](\d{4})', page
        )
        return int(match.group(1)) if match else None

    @staticmethod
    def _find_license_url(config_text: str, clip_id: str) -> Optional[str]:
        match = re.search(
            rf"https://player\.vimeo\.com/video/{clip_id}/license/widevine[^\"'\s<>\\]+",
            config_text,
        )
        if match:
            return match.group(0).replace("\\u0026", "&").replace("\\/", "/")
        return None

    @staticmethod
    def _extract_dash_urls(vimeo_config: dict, clip_id: str) -> tuple[Optional[str], Optional[str]]:
        request = vimeo_config.get("request", {})
        files = request.get("files", {})

        dash = files.get("dash", {})
        cdns = dash.get("cdns", {})
        default_cdn = dash.get("default_cdn")
        mpd_url: Optional[str] = None
        if default_cdn and default_cdn in cdns:
            mpd_url = cdns[default_cdn].get("url")
        if not mpd_url and cdns:
            mpd_url = next(iter(cdns.values())).get("url")
        if not mpd_url:
            mpd_url = dash.get("url")

        # License URL lives at request.drm.cdms.widevine.license_url
        widevine = (
            request.get("drm", {}).get("cdms", {}).get("widevine", {})
        )
        license_url: Optional[str] = widevine.get("license_url") if isinstance(widevine, dict) else None

        return mpd_url, license_url
