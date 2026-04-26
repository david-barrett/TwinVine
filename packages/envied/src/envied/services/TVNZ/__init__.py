from __future__ import annotations

import base64
import concurrent.futures
import json
import re
import time
from collections.abc import Generator
from http.cookiejar import MozillaCookieJar
from typing import Any, Optional
from urllib.parse import urlparse

import click
import jwt
from click import Context
from langcodes import Language
from lxml import etree
from envied.core import __version__
from envied.core.cdm.detect import is_playready_cdm
from envied.core.config import config
from envied.core.credential import Credential
from envied.core.manifests.dash import DASH
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series
from envied.core.tracks import Chapter, Chapters, Tracks


class TVNZ(Service):
    """
    \b
    Service code for TVNZ streaming service (https://www.tvnz.co.nz).

    \b
    Version: 2.0.2
    Author: stabbedbybrick
    Authorization: tokens
    Robustness:
      Widevine:
        L3: 1080p, DDP5.1
      PlayReady:
        SL2000: 1080p, DDP5.1

    \b
    Tips:
        - Input can be comlete URL or path:
          SHOW: /tvseries/the-rookie
          EPISODE: /player/tvepisode/the-rookie-1
          MOVIE: /movie/stand-by-me
          EVENT: /player/event/bula-fc-v-auckland-fc
          HIGHLIGHT: /player/sporthighlight/sheep-dog-trials
          CLIP: /player/newsclip/drone-captures-slip-that-caused-evacuations-blocked-auckland-road

    \b
    Notes:
        TVNZ has moved to an OTP-only login system, with no username/password and no cookies.
        Auth sessions are stored in the browser's local storage, so they need to be extracted once
        before being cached for future use.
        There are many ways to extract it, but the easiest is with a browser extension, such as
        "Cookie & Storage Exporter" or similar. Name the exported JSON file 'local_storage.json' and place it 
        in the `TwinVine/Cache/TVNZ` directory and it'll be added to cache on the next run.
        Do note that the session can't be shared between browser and script, and will invalidate the other session
        when tokens are refreshed. It's recommended to use a separate account for ripping purposes.
    """

    GEOFENCE = ("nz",)
    TITLE_RE = r"^(?:https?://(?:www\.)?tvnz\.co\.nz)?/(?:player/)?(tvseries|tvepisode|movie|event|sporthighlight|newsclip|sportclip)/([^/]+)$"

    @staticmethod
    @click.command(name="TVNZ", short_help="https://tvnz.co.nz", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> TVNZ:
        return TVNZ(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        super().__init__(ctx)
        self.title = title

        self.drm_system = "playready" if is_playready_cdm(ctx.obj.cdm) else "widevine"

        self.profile = ctx.parent.params.get("profile") or "default"
        self.session.headers.update(self.config["headers"])

    def search(self) -> Generator[SearchResult, None, None]:
        params = {
            "mode": "detail",
            "st": "published",
            "term": self.title,
            "pageNumber": "1",
            "pageSize": "50",
            "reg": "nz",
            "dt": "web",
            "client": "tvnz-tvnz-web",
            "pf": "Regular",
            "allowpg": "true",
        }

        response = self.session.get(self.config["endpoints"]["search"], params=params).json()
        if response.get("header", {}).get("message", "").lower() != "success":
            self.log.error(f"Failed to get search results for '{self.title}'")
            return

        results = response.get("data", [])

        for result in results:
            content_type = result.get("cty")
            content_id = result.get("nu")
            title = result.get("lon", [{"n": ""}])[0].get("n")
            synopsis = result.get("losd", [{"n": ""}])[0].get("n")
            yield SearchResult(
                id_=f"https://tvnz.co.nz/{content_type}/{content_id}",
                title=title,
                description=synopsis,
                label=content_type,
                url=f"https://tvnz.co.nz/{content_type}/{content_id}",
            )

    def authenticate(self, cookies: Optional[MozillaCookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)

        user_tokens, session_tokens = self._get_cached_tokens(self.profile)

        # If cache is missing or invalid, fallback to local storage
        if not user_tokens or not session_tokens:
            user_tokens, session_tokens = self._fetch_and_cache_local_storage(self.profile)

        self.access_token = user_tokens["access_token"]
        self.device_ref = user_tokens["deviceref"]
        self.contact_id = user_tokens["contact_id"]
        self.xauthorization = session_tokens["xauthorization"]

        self.oauth_token = self._get_oauth_token()
        self.secret = self._register_app(self.oauth_token, self.xauthorization, self.device_ref)

    def get_titles(self) -> Movies | Series:
        match = re.match(self.TITLE_RE, self.title)
        if not match:
            raise ValueError(f"Invalid title: {self.title}")

        content_type, _ = match.groups()
        title_path = urlparse(self.title).path.replace("/player/", "/")

        if content_type == "tvseries":
            return self._get_series(title_path)
        elif content_type == "tvepisode":
            return self._get_episode(title_path)
        elif content_type == "movie":
            return self._get_movie(title_path)
        elif content_type in ("event", "sporthighlight", "newsclip", "sportclip"):
            return self._get_single(title_path)
        else:
            raise ValueError(f"Unsupported content type: {content_type}")

    def get_tracks(self, title: Movie | Episode) -> Tracks:
        device_token = self._get_device_token(self.secret, self.device_ref)

        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "authorization": f"Bearer {self.oauth_token}",
            "content-type": "application/json",
            "origin": "https://tvnz.co.nz",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            "x-authorization": f"{self.xauthorization}",
            "x-client-id": "tvnz-tvnz-web",
            "x-device-id": f"{device_token}",
            "x-device-type": "web",
        }

        json_data = {
            "deviceName": "web",
            "deviceId": f"{self.device_ref}",
            "contentId": title.id,
            "contentTypeId": "vod",
            "catalogType": title.data.get("cty"),
            "mediaFormat": "dash",
            "drm": self.drm_system,
            "delivery": "streaming",
            "disableSsai": "true",
            "deviceManufacturer": "web",
            "deviceModelName": "Chrome browser on Windows",
            "deviceModelNumber": "Chrome",
            "deviceOs": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            "supportedAudioCodecs": "mp4a",
            "supportedVideoCodecs": "avc,hevc,av01",
            "supportedMaxWVSecurityLevel": "L3",
            "deviceToken": f"{device_token}",
            "urlParameters": {
                "vpa": "click",
                "rdid": f"{self.device_ref}",
                "is_lat": "0",
                "npa": "0",
                "idtype": "dpid",
                "endpoint": "web",
                "endpoint-group": "desktop",
                "endpoint_detail": "desktop",
            },
        }

        response = self.session.post(
            url=self.config["endpoints"]["authorize"],
            headers=headers,
            json=json_data,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("header", {}).get("message", "").lower() != "success":
            raise ConnectionError(f"Failed to authorize playback: {data}")
        
        title.data["license_url"] = data.get("data", {}).get("licenseUrl")
        title.data["markers"] = title.data.get("mar")

        source_manifest = data.get("data", {}).get("contentUrl").split("?")[0]

        # Temporary workaround for non-development branch
        if __version__ < "5.0.0":
            manifest = self._modify_transfer(source_manifest)
            tracks = DASH.from_text(url=source_manifest, text=manifest).to_tracks(language=title.language)

        else:
            tracks = DASH.from_url(source_manifest, self.session).to_tracks(language=title.language)

        for track in tracks.audio:
            role = track.data["dash"]["adaptation_set"].find("Role")
            if role is not None and role.get("value") in ["description", "alternative", "alternate"]:
                track.descriptive = True

        return tracks

    def get_chapters(self, title: Movie | Episode) -> Chapters:
        if not (markers := title.data.get("markers")):
            return Chapters()
        
        chapters = []
        for marker in markers:
            if marker.get("t", "").lower() == "postplay":
                chapters.append(Chapter(name="Credits", timestamp=marker.get("m_st") * 1000))
            else:
                chapters.append(Chapter(name=marker.get("t"), timestamp=marker.get("m_st") * 1000))
                chapters.append(Chapter(timestamp=marker.get("m_ed")))

        if not any(c.timestamp == "00:00:00.000" for c in chapters):
            chapters.append(Chapter(timestamp=0))

        return sorted(chapters, key=lambda x: x.timestamp)

    def get_widevine_service_certificate(self, *, challenge: bytes, title: Episode | Movie, track: Any) -> bytes | str | None:
        return None

    def get_widevine_license(self, *, challenge: bytes, title: Episode | Movie, track: Any) -> bytes | str | None:
        if not (license_url := title.data.get("license_url")):
            return None

        headers = {
            'accept': '*/*',
            'authorization': 'Bearer {}'.format(self.oauth_token),
            'origin': 'https://tvnz.co.nz',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36',
        }
        r = self.session.post(url=license_url, headers=headers, data=challenge)
        r.raise_for_status()

        return r.content
    
    def get_playready_license(self, *, challenge: bytes, title: Episode | Movie, track: Any) -> bytes | str | None:
        if not (license_url := title.data.get("license_url")):
            return None

        headers = {
            'accept': '*/*',
            'authorization': 'Bearer {}'.format(self.oauth_token),
            'origin': 'https://tvnz.co.nz',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36',
        }
        r = self.session.post(url=license_url, headers=headers, data=challenge)
        r.raise_for_status()

        return r.content

    # Service-specific methods

    def _fetch_season_episodes(self, series_id: str, season_id: str) -> list[Episode]:
        try:
            response = self.session.get(
                url=self.config["endpoints"]["episodes"].format(series_id=series_id),
                params={
                    "seasonId": season_id,
                    "pageNumber": "1",
                    "pageSize": "99",
                    "sortBy": "epnum",
                    "sortOrder": "asc",
                    "reg": "nz",
                    "dt": "web",
                    "client": "tvnz-tvnz-web",
                    "pf": "Regular",
                    "allowpg": "true",
                },
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            
            if not (episodes := data.get("data")):
                self.log.error(f"Failed to get episodes for season {season_id}")
                return []
            
            return [
                Episode(
                    id_=episode.get("nu"),
                    service=self.__class__,
                    title=episode.get("lostl")[0].get("n"),
                    season=episode.get("snum"),
                    number=episode.get("epnum"),
                    name=episode.get("lodn")[0].get("n"),
                    # year=episode.get("oadt", "").split("-")[0],
                    language=Language.find(episode.get("aud_lg", ["English"])[0]).to_alpha3(),
                    data=episode,
                )
                for episode in episodes
            ]
        except Exception as e:
            self.log.error(f"Failed to fetch season {season_id}: {e}")
            return []

    def _get_series(self, title_path: str) -> Series:
        response = self.session.get(
            url=self.config["endpoints"]["catalog"] + title_path,
            params={
                "reg": "nz",
                "dt": "web",
                "client": "tvnz-tvnz-web",
                "pf": "Regular",
                "allowpg": "true",
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        if not (series_id := data.get("data", {}).get("id")):
            raise ValueError(f"Failed to get series ID: {data}")

        response = self.session.get(
            url=self.config["endpoints"]["seasons"].format(series_id=series_id),
            params={
                "pageNumber": "1",
                "pageSize": "99",
                "sortBy": "asc",
                "sortOrder": "desc",
                "reg": "nz",
                "dt": "web",
                "client": "tvnz-tvnz-web",
                "pf": "Regular",
                "allowpg": "true",
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        seasons = [x.get("id") for x in data["data"]]
        if not seasons:
            raise ValueError(f"Failed to get seasons: {data}")
        
        all_episodes = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [
                executor.submit(self._fetch_season_episodes, series_id, season) 
                for season in seasons
            ]
            for future in futures:
                all_episodes.extend(future.result())
                
        return Series(all_episodes)

    def _get_movie(self, title_path: str) -> Movies:
        response = self.session.get(
            url=self.config["endpoints"]["catalog"] + title_path,
            params={
                "reg": "nz",
                "dt": "web",
                "client": "tvnz-tvnz-web",
                "pf": "Regular",
                "allowpg": "true",
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        if not (video := data.get("data")):
            raise ValueError(f"Failed to get movie: {data}")

        movies = [
            Movie(
                id_=video.get("nu"),
                service=self.__class__,
                name=video.get("lodn", [{"n": ""}])[0].get("n"),
                year=None,
                language=Language.find(video.get("aud_lg", ["English"])[0]).to_alpha3(),
                data=video,
            )
        ]

        return Movies(movies)

    def _get_episode(self, title_path: str) -> Series:
        response = self.session.get(
            url=self.config["endpoints"]["catalog"] + title_path,
            params={
                "reg": "nz",
                "dt": "web",
                "client": "tvnz-tvnz-web",
                "pf": "Regular",
                "allowpg": "true",
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        if not (video := data.get("data")):
            raise ValueError(f"Failed to get episode: {data}")

        episodes = [
            Episode(
                id_=video.get("nu"),
                service=self.__class__,
                title=video.get("lostl")[0].get("n"),
                season=video.get("snum"),
                number=video.get("epnum"),
                name=video.get("lodn", [{"n": ""}])[0].get("n"),
                # year=video.get("oadt", "").split("-")[0],
                language=Language.find(video.get("aud_lg", ["English"])[0]).to_alpha3(),
                data=video,
            )
        ]

        return Series(episodes)
    
    def _get_single(self, title_path: str) -> Movies:
        response = self.session.get(
            url=self.config["endpoints"]["catalog"] + title_path,
            params={
                "reg": "nz",
                "dt": "web",
                "client": "tvnz-tvnz-web",
                "pf": "Regular",
                "allowpg": "true",
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        if not (video := data.get("data")):
            raise ValueError(f"Failed to get episode: {data}")
        
        events = [
            Movie(
                id_=video.get("nu"),
                service=self.__class__,
                name=video.get("lodn", [{"n": ""}])[0].get("n"),
                year=video.get("r"),
                language=Language.find(video.get("aud_lg", ["English"])[0]).to_alpha3(),
                data=video,
            )
        ]

        return Movies(events)
        
    @staticmethod    
    def _get_device_token(secret_b64: str, device_id: str) -> str:
        secret_bytes = base64.b64decode(secret_b64)

        payload = {
            "deviceId": device_id,
            "aud": "playback-auth-service",
            "iat": int(time.time()),
            "exp": int(time.time()) + 30
        }

        device_token = jwt.encode(payload, secret_bytes, algorithm="HS256")
        return device_token
        
    def _get_entitlements(self, access_token: str, contact_id: str):
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-US,en;q=0.9",
            "authorization": f"Bearer {access_token}",
            "content-type": "application/json",
            "origin": "https://tvnz.co.nz",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        }

        json_data = {
            "GetEntitlementsRequestMessage": {
                "contactID": contact_id,
                **self.config["contact"],
                "returnUpgradableFlag": "true",
                "returnProductAttributes": "true",
            },
        }

        response = self.session.post(
            url=self.config["endpoints"]["entitlements"],
            headers=headers,
            json=json_data,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("GetEntitlementsResponseMessage").get("message", "").lower() != "success":
            raise ConnectionError(f"Failed to get entitlements: {data}")
        
        token = data.get("GetEntitlementsResponseMessage").get("ovatToken")
        expiry = data.get("GetEntitlementsResponseMessage").get("ovatTokenExpiry")
        if not token:
            raise ValueError(f"Failed to get entitlements: {data}")
        
        return token, expiry

    def _get_contact_id(self, access_token: str):
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-US,en;q=0.9",
            "authorization": f"Bearer {access_token}",
            "content-type": "application/json",
            "origin": "https://tvnz.co.nz",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        }

        json_data = {"GetContactRequestMessage": {**self.config["contact"]}}

        response = self.session.post(
            url=self.config["endpoints"]["contact"],
            headers=headers,
            json=json_data,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("GetContactResponseMessage", {}).get("message", "").lower() != "success":
            raise ConnectionError(f"Failed to get contact: {data}")
        
        return data["GetContactResponseMessage"]["contactMessage"][0]["contactID"]

    def _get_oauth_token(self) -> str:
        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
            "origin": "https://tvnz.co.nz",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        }

        data = {
            **self.config["web_client"],
            "grant_type": "client_credentials",
            "audience": "edge-service",
            "scope": "offline openid",
        }

        response = self.session.post(
            url=self.config["endpoints"]["oauth"],
            headers=headers,
            data=data,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        if not (token := data.get("access_token")):
            raise ValueError("Failed to get OAuth token")

        return token

    def _register_app(self, oauth_token: str, xauth: str, deviceref: str) -> str:
        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "authorization": f"Bearer {oauth_token}",
            "content-type": "text/plain;charset=UTF-8",
            "origin": "https://tvnz.co.nz",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            "x-authorization": f"{xauth}",
            "x-client-id": "tvnz-tvnz-web",
        }

        response = self.session.post(
            url=self.config["endpoints"]["register"],
            headers=headers,
            data=json.dumps({"uniqueId": deviceref}),
            timeout=30,
        )
        response.raise_for_status()
        registration = response.json()

        if not (secret := registration.get("data", {}).get("secret")):
            raise ValueError(f"Failed to register app: {registration}")
        
        return secret
    
    def _refresh_user_tokens(self, tokens: dict) -> tuple[dict, int]:
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "origin": "https://tvnz.co.nz",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        }

        json_data = {
            "RefreshTokenRequestMessage": {
                **self.config["contact"],
                "refreshToken": tokens.get("refresh_token"),
            },
        }

        response = self.session.post(
            url=self.config["endpoints"]["refresh"],
            headers=headers,
            json=json_data,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("RefreshTokenResponseMessage", {}).get("message", "").lower() != "success":
            raise ConnectionError(f"Failed to refresh user tokens: {data}")
        
        access_token = data["RefreshTokenResponseMessage"]["accessToken"]
        refresh_token = data["RefreshTokenResponseMessage"]["refreshToken"]
        expiry = data["RefreshTokenResponseMessage"]["expiresIn"]

        return {"access_token": access_token, "refresh_token": refresh_token}, expiry
    
    def _refresh_session_tokens(self, tokens: dict) -> tuple[dict, int]:
        xauthorization, xexpiry = self._get_entitlements(tokens.get("access_token"), tokens.get("contact_id"))
        return {"x-authorization": xauthorization}, xexpiry

    def _get_cached_tokens(self, profile: str) -> tuple[dict | None, dict | None]:
        user_tokens = self.cache.get(f"{profile}_user_tokens")
        session_tokens = self.cache.get(f"{profile}_session_tokens")

        if not (user_tokens and session_tokens):
            return None, None

        if not user_tokens.expired:
            self.log.info(" + Using cached user tokens")
            ptokens = user_tokens.data
        else:
            self.log.info(" + Refreshing cached user tokens..")
            ptokens, pexpiry = self._refresh_user_tokens(user_tokens.data)
            ptokens["deviceref"] = user_tokens.data["deviceref"]
            ptokens["contact_id"] = user_tokens.data["contact_id"]
            user_tokens.set(ptokens, expiration=int(pexpiry) - 3600)
        
        if not session_tokens.expired:
            self.log.info(" + Using cached session tokens")
            xtokens = session_tokens.data
        else:
            self.log.info(" + Refreshing cached session tokens..")
            xtokens, xexpiry = self._refresh_session_tokens(session_tokens.data)
            session_tokens.set(xtokens, expiration=int(xexpiry) - 3600)

        return ptokens, xtokens

    def _fetch_and_cache_local_storage(self, profile: str) -> tuple[dict, dict]:
        self.log.info(" + Fetching tokens from local storage JSON..")
        cache_dir = config.directories.cache / "TVNZ"
        storage = next((
            f for f in cache_dir.rglob("*.json")
            if f.is_file() and any(t in f.name for t in ("localStorage", "local_storage"))
            ),None,)
        if not storage:
            raise EnvironmentError("'localStorage' not found. \nRun 'envied. dl TVNZ --help' for more information.")
        
        try:
            user = json.loads(storage.read_text())
        except json.JSONDecodeError:
            raise ValueError(f"'{storage}' is corrupted. \nRun 'envied. dl TVNZ --help' for more information.")
        
        access_token = user.get("accessToken")
        refresh_token = user.get("refreshToken")
        device_ref = user.get("deviceref")

        for token in (access_token, refresh_token, device_ref):
            if not token:
                raise ValueError(
                    f"Required token '{token}' is missing from '{storage}'. \nRun 'envied. dl TVNZ --help' for more information."
                )
        
        pexpiry = jwt.decode(access_token, options={"verify_signature": False}).get("exp")
        contact_id = self._get_contact_id(access_token)
        xauthorization, xexpiry = self._get_entitlements(access_token, contact_id)

        ptokens = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "deviceref": device_ref,
            "contact_id": contact_id,
        }
        xtokens = {
            "xauthorization": xauthorization,
        }

        user_tokens = self.cache.get(f"{profile}_user_tokens")
        session_tokens = self.cache.get(f"{profile}_session_tokens")

        user_tokens.set(ptokens, expiration=int(pexpiry) - 3600)
        session_tokens.set(xtokens, expiration=int(xexpiry) - 3600)

        return ptokens, xtokens
    
    def _modify_transfer(self, source_manifest: str) -> str:
        """
        Change transfer type to "2" until dev branch is merged
        """
        manifest = DASH.from_url(source_manifest, self.session).manifest
        periods = manifest.findall("Period")
        for period in periods:
            for adaptation_set in period.findall("AdaptationSet"):
                for prop in adaptation_set.findall("SupplementalProperty"):
                    if (
                        prop is not None
                        and prop.get("schemeIdUri") == "urn:mpeg:mpegB:cicp:TransferCharacteristics"
                    ):
                        prop.set("value", "2")

        return etree.tostring(manifest, encoding="unicode")
