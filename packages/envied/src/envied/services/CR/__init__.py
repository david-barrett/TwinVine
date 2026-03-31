import re
import time
import uuid
from threading import Lock
from typing import Generator, Optional, Union

import click
import jwt
from langcodes import Language

from envied.core.manifests import DASH
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.session import session
from envied.core.titles import Episode, Series
from envied.core.tracks import Chapters, Tracks
from envied.core.tracks.chapter import Chapter
from envied.core.tracks.subtitle import Subtitle


class CR(Service):
    """
    Service code for Crunchyroll streaming service (https://www.crunchyroll.com).

    \b
    Version: 3.0.1
    Author: sp4rk.y
    Date: 2026-03-26
    Authorization: Credentials
    Robustness:
        Widevine:
            L3: 1080p, AAC2.0

    \b
    Tips:
        - Input should be complete URL or series ID
            https://www.crunchyroll.com/series/GRMG8ZQZR/series-name OR GRMG8ZQZR
        - Supports multiple audio and subtitle languages
        - Device ID is cached for consistent authentication across runs

    \b
    Notes:
        - Emulates Android TV client (v3.58.0) with Playback API v3
        - Uses password-based authentication with refresh token caching
        - Refresh tokens are cached for 30 days for cross-session reuse
        - Manages concurrent stream limits automatically
    """

    TITLE_RE = r"^(?:https?://(?:www\.)?crunchyroll\.com/(?:series|watch)/)?(?P<id>[A-Z0-9]+)"
    LICENSE_LOCK = Lock()
    MAX_CONCURRENT_STREAMS = 3
    ACTIVE_STREAMS: list[tuple[str, str]] = []

    @staticmethod
    def get_session():
        return session("okhttp4")

    @staticmethod
    @click.command(name="CR", short_help="https://crunchyroll.com")
    @click.argument("title", type=str, required=True)
    @click.pass_context
    def cli(ctx, **kwargs) -> "CR":
        return CR(ctx, **kwargs)

    def __init__(self, ctx, title: str):
        self.title = title
        self.account_id: Optional[str] = None
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.profile_id: Optional[str] = None
        self.token_expiration: Optional[int] = None
        self.anonymous_id = str(uuid.uuid4())

        super().__init__(ctx)

        device_cache_key = "cr_device_id"
        cached_device = self.cache.get(device_cache_key)

        if cached_device and not cached_device.expired:
            self.device_id = cached_device.data["device_id"]
        else:
            self.device_id = str(uuid.uuid4())
            cached_device.set(
                data={"device_id": self.device_id},
                expiration=60 * 60 * 24 * 365 * 10,
            )

        self.device_name = self.config.get("device", {}).get("name", "SHIELD Android TV")
        self.device_type = self.config.get("device", {}).get("type", "ANDROIDTV")

        self.session.headers.update(self.config.get("headers", {}))
        self.session.headers["etp-anonymous-id"] = self.anonymous_id

    @property
    def auth_header(self) -> dict:
        """Return authorization header dict."""
        return {"authorization": f"Bearer {self.access_token}"}

    def ensure_authenticated(self) -> None:
        """Check if token is expired and re-authenticate if needed."""
        if not self.token_expiration:
            cache_key = f"cr_auth_token_{self.credential.sha1 if self.credential else 'default'}"
            cached = self.cache.get(cache_key)

            if cached and not cached.expired:
                self.access_token = cached.data["access_token"]
                self.account_id = cached.data.get("account_id")
                self.profile_id = cached.data.get("profile_id")
                self.refresh_token = cached.data.get("refresh_token")
                self.token_expiration = cached.data.get("token_expiration")
                self.session.headers.update(self.auth_header)
            else:
                self.authenticate(credential=self.credential)
                return

        current_time = int(time.time())
        if current_time >= (self.token_expiration - 60):
            if self.refresh_token:
                self._refresh_access_token()
            else:
                self.authenticate(credential=self.credential)

    def authenticate(self, cookies=None, credential=None) -> None:
        """Authenticate using username and password credentials, with refresh token support."""
        super().authenticate(cookies, credential)

        cache_key = f"cr_auth_token_{credential.sha1 if credential else 'default'}"
        cached = self.cache.get(cache_key)

        if cached and not cached.expired:
            self.access_token = cached.data["access_token"]
            self.account_id = cached.data.get("account_id")
            self.profile_id = cached.data.get("profile_id")
            self.refresh_token = cached.data.get("refresh_token")
            self.token_expiration = cached.data.get("token_expiration")
        else:
            refresh_cache_key = f"cr_refresh_token_{credential.sha1 if credential else 'default'}"
            refresh_cached = self.cache.get(refresh_cache_key)

            if refresh_cached and not refresh_cached.expired and refresh_cached.data.get("refresh_token"):
                self.refresh_token = refresh_cached.data["refresh_token"]
                try:
                    self._refresh_access_token()
                    self._cache_auth(cache_key)
                    self.session.headers.update(self.auth_header)

                    if self.ACTIVE_STREAMS:
                        self.ACTIVE_STREAMS.clear()

                    try:
                        self.clear_all_sessions()
                    except Exception as e:
                        self.log.warning(f"Failed to clear previous sessions: {e}")
                    return
                except Exception:
                    self.log.warning("Refresh token expired or invalid, falling back to password login")
                    self.refresh_token = None

            if not credential:
                raise ValueError("No credential provided for authentication")

            # Get anonymous client token first (required before password grant)
            self._get_client_token()

            response = self.session.post(
                url=self.config["endpoints"]["token"],
                headers={
                    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "request-type": "SignIn",
                },
                data={
                    "grant_type": "password",
                    "username": credential.username,
                    "password": credential.password,
                    "scope": "offline_access",
                    "client_id": self.config["client"]["id"],
                    "client_secret": self.config["client"]["secret"],
                    "device_type": self.device_type,
                    "device_id": self.device_id,
                    "device_name": self.device_name,
                },
            )

            if response.status_code != 200:
                try:
                    error_data = response.json()
                    error_msg = error_data.get("error", "Unknown error")
                    error_code = error_data.get("code", "")
                    raise ValueError(f"Login failed: {response.status_code} - {error_msg} ({error_code})")
                except ValueError:
                    raise
                except Exception:
                    raise ValueError(f"Login failed: {response.status_code} - {response.text}")

            self._apply_token_response(response.json())
            self._cache_auth(cache_key)

        self.session.headers.update(self.auth_header)

        if self.ACTIVE_STREAMS:
            self.ACTIVE_STREAMS.clear()

        try:
            self.clear_all_sessions()
        except Exception as e:
            self.log.warning(f"Failed to clear previous sessions: {e}")

    def _apply_token_response(self, token_data: dict) -> None:
        """Extract and store auth fields from a token response."""
        self.access_token = token_data["access_token"]
        self.refresh_token = token_data.get("refresh_token", self.refresh_token)
        self.account_id = token_data.get("account_id", self.account_id)
        self.profile_id = token_data.get("profile_id", self.profile_id)

        try:
            decoded_token = jwt.decode(self.access_token, options={"verify_signature": False})
            self.token_expiration = decoded_token.get("exp")
        except Exception:
            self.token_expiration = int(time.time()) + token_data.get("expires_in", 300)

    def _cache_auth(self, cache_key: str) -> None:
        """Cache current auth state (access token + refresh token)."""
        ttl = 300
        if isinstance(self.token_expiration, int):
            remaining = self.token_expiration - int(time.time())
            if remaining > 0:
                ttl = remaining

        cached = self.cache.get(cache_key)
        cached.set(
            data={
                "access_token": self.access_token,
                "account_id": self.account_id,
                "profile_id": self.profile_id,
                "refresh_token": self.refresh_token,
                "token_expiration": self.token_expiration,
            },
            expiration=ttl,
        )

        # Cache refresh token separately with a long TTL for cross-session reuse
        if self.refresh_token:
            refresh_cache_key = cache_key.replace("cr_auth_token_", "cr_refresh_token_")
            refresh_cached = self.cache.get(refresh_cache_key)
            refresh_cached.set(
                data={"refresh_token": self.refresh_token},
                expiration=60 * 60 * 24 * 30,  # 30 days
            )

    def _get_client_token(self) -> None:
        """Get an anonymous client token. Required before password grant on fresh sessions."""
        response = self.session.post(
            url=self.config["endpoints"]["token"],
            headers={
                "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
            data={
                "grant_type": "client_id",
                "scope": "offline_access",
                "client_id": self.config["client"]["id"],
                "client_secret": self.config["client"]["secret"],
            },
        )
        if response.status_code == 200:
            token_data = response.json()
            self.access_token = token_data["access_token"]
            self.session.headers.update(self.auth_header)
        else:
            self.log.warning(f"Failed to get anonymous client token: {response.status_code}")

    def _refresh_access_token(self) -> None:
        """Refresh the access token using the stored refresh token."""
        if not self.refresh_token:
            raise ValueError("No refresh token available")

        response = self.session.post(
            url=self.config["endpoints"]["token"],
            headers={
                "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "scope": "offline_access",
                "client_id": self.config["client"]["id"],
                "client_secret": self.config["client"]["secret"],
                "device_type": self.device_type,
                "device_id": self.device_id,
                "device_name": self.device_name,
            },
        )

        if response.status_code != 200:
            self.refresh_token = None
            raise ValueError(f"Token refresh failed: {response.status_code}")

        self._apply_token_response(response.json())

        cache_key = f"cr_auth_token_{self.credential.sha1 if self.credential else 'default'}"
        self._cache_auth(cache_key)
        self.session.headers.update(self.auth_header)

    def get_titles(self) -> Union[Series]:
        """Fetch series and episode information."""
        series_id = self.parse_series_id(self.title)

        series_http = self.session.get(
            url=self.config["endpoints"]["series"].format(series_id=series_id),
            params={"locale": self.config["params"]["locale"]},
        )
        series_response = series_http.json()

        if "error" in series_response:
            raise ValueError(f"Series not found: {series_id} - {series_response.get('error')}")

        series_data = (
            series_response.get("data", [{}])[0] if isinstance(series_response.get("data"), list) else series_response
        )
        series_title = series_data.get("title", "Unknown Series")

        seasons_http = self.session.get(
            url=self.config["endpoints"]["seasons"].format(series_id=series_id),
            params={
                "locale": self.config["params"]["locale"],
                "preferred_audio_language": self.config.get("params", {}).get("preferred_audio_language", "en-US"),
            },
        )
        seasons_response = seasons_http.json()

        seasons_data = seasons_response.get("data", [])

        if not seasons_data:
            raise ValueError(f"No seasons found for series: {series_id}")

        used_season_numbers: set[int] = set()
        season_id_to_number: dict[str, int] = {}

        all_episode_data = []
        special_episodes = []

        for season in seasons_data:
            season_id = season["id"]
            season_number = season.get("season_number", 0)

            effective_season_number = season_number
            if isinstance(season_number, int) and season_number > 0:
                if season_number in used_season_numbers:
                    candidate = season_number + 1
                    while candidate in used_season_numbers:
                        candidate += 1
                    effective_season_number = candidate
                used_season_numbers.add(effective_season_number)

            season_id_to_number[season_id] = effective_season_number

            episodes_http = self.session.get(
                url=self.config["endpoints"]["season_episodes"].format(season_id=season_id),
                params={"locale": self.config["params"]["locale"]},
            )
            episodes_response = episodes_http.json()

            episodes_data = episodes_response.get("data", [])

            for episode_data in episodes_data:
                episode_number = episode_data.get("episode_number")

                if episode_number is None or isinstance(episode_number, float):
                    special_episodes.append(episode_data)

                all_episode_data.append((episode_data, effective_season_number))

        if not all_episode_data:
            raise ValueError(f"No episodes found for series: {series_id}")

        series_year = None
        if all_episode_data:
            first_episode_data = all_episode_data[0][0]
            first_air_date = first_episode_data.get("episode_air_date")
            if first_air_date:
                series_year = int(first_air_date[:4])

        special_episodes.sort(key=lambda x: x.get("episode_air_date", ""))
        special_episode_numbers = {ep["id"]: idx + 1 for idx, ep in enumerate(special_episodes)}
        episodes = []
        season_episode_counts = {}

        for episode_data, season_number in all_episode_data:
            episode_number = episode_data.get("episode_number")

            if episode_number is None or isinstance(episode_number, float):
                final_season = 0
                final_number = special_episode_numbers[episode_data["id"]]
            else:
                final_season = season_number
                if final_season not in season_episode_counts:
                    season_episode_counts[final_season] = 0

                season_episode_counts[final_season] += 1
                final_number = season_episode_counts[final_season]

            original_language = None
            versions = episode_data.get("versions", [])
            for version in versions:
                if "main" in version.get("roles", []):
                    original_language = version.get("audio_locale")
                    break

            episode = Episode(
                id_=episode_data["id"],
                service=self.__class__,
                title=series_title,
                season=final_season,
                number=final_number,
                name=episode_data.get("title"),
                year=series_year,
                language=original_language,
                description=episode_data.get("description"),
                data=episode_data,
            )
            episodes.append(episode)

        return Series(episodes)

    def set_track_metadata(
        self, tracks: Tracks, episode_id: str, is_original: bool, audio_locale: Optional[str] = None
    ) -> None:
        """Set metadata for video and audio tracks."""
        for video in tracks.videos:
            video.needs_repack = True
            video.data["episode_id"] = episode_id
            video.is_original_lang = is_original
            if audio_locale:
                video.data["audio_locale"] = audio_locale
        for audio in tracks.audio:
            audio.data["episode_id"] = episode_id
            audio.is_original_lang = is_original
            if audio_locale:
                audio.data["audio_locale"] = audio_locale

    def get_tracks(self, title: Episode) -> Tracks:
        """Fetch video, audio, and subtitle tracks for an episode."""
        self.ensure_authenticated()

        episode_id = title.id

        if self.ACTIVE_STREAMS:
            self.ACTIVE_STREAMS.clear()

        self.clear_all_sessions()

        endpoints_to_try = ["playback", "playback_download"]

        preferred_audio = self.config.get("params", {}).get("preferred_audio_language", "en-US")
        initial_response = self.get_playback_data(
            episode_id, track_stream=False, endpoint_key="playback", audio_locale=preferred_audio
        )
        versions = initial_response.get("versions", [])

        if not versions:
            self.log.warning("No versions found in playback response, using single version")
            versions = [{"audio_locale": initial_response.get("audioLocale", "ja-JP")}]

        tracks = None

        for idx, version in enumerate(versions):
            audio_locale = version.get("audio_locale")
            version_guid = version.get("guid")
            is_original = version.get("original", False)

            if not audio_locale:
                continue

            request_episode_id = version_guid if version_guid else episode_id

            for endpoint_key in endpoints_to_try:
                try:
                    if idx == 0 and not version_guid and endpoint_key == "playback":
                        version_response = initial_response
                        version_token = version_response.get("token")
                    else:
                        if idx == 1 and not versions[0].get("guid") and endpoint_key == "playback":
                            initial_token = initial_response.get("token")
                            if initial_token:
                                self.close_stream(episode_id, initial_token)

                        try:
                            version_response = self.get_playback_data(
                                request_episode_id,
                                track_stream=False,
                                endpoint_key=endpoint_key,
                                audio_locale=audio_locale,
                            )
                        except ValueError as e:
                            self.log.warning(
                                f"Could not get playback info for audio {audio_locale} from {endpoint_key}: {e}"
                            )
                            continue

                        version_token = version_response.get("token")

                    hard_subs = version_response.get("hardSubs", {})

                    root_url = version_response.get("url")

                    if root_url and "/clean/" in root_url:
                        dash_url = root_url
                    elif "none" in hard_subs:
                        dash_url = hard_subs["none"].get("url")
                    elif "fr-FR" in hard_subs:
                        dash_url = hard_subs["fr-FR"].get("url")
                    elif hard_subs:
                        first_key = list(hard_subs.keys())[0]
                        dash_url = hard_subs[first_key].get("url")
                    else:
                        dash_url = None

                    if not dash_url:
                        if version_token:
                            self.close_stream(request_episode_id, version_token)
                        continue

                    try:
                        version_tracks = DASH.from_url(
                            url=dash_url,
                            session=self.session,
                        ).to_tracks(language=audio_locale)

                        if tracks is None:
                            tracks = version_tracks
                            self.set_track_metadata(tracks, request_episode_id, is_original, audio_locale)
                        else:
                            self.set_track_metadata(version_tracks, request_episode_id, is_original, audio_locale)
                            for video in version_tracks.videos:
                                if not any(v.id == video.id for v in tracks.videos):
                                    tracks.add(video)
                            for audio in version_tracks.audio:
                                existing_audio = next((a for a in tracks.audio if a.language == audio.language), None)
                                if existing_audio is None or (
                                    hasattr(audio, "bitrate")
                                    and hasattr(existing_audio, "bitrate")
                                    and audio.bitrate > existing_audio.bitrate
                                ):
                                    tracks.add(audio)
                                elif existing_audio is None:
                                    tracks.add(audio)

                    except Exception as e:
                        self.log.warning(
                            f"Failed to parse DASH manifest for audio {audio_locale} from {endpoint_key}: {e}"
                        )
                        if version_token:
                            self.close_stream(request_episode_id, version_token)
                        continue

                    if is_original and endpoint_key == "playback":
                        captions = version_response.get("captions", {})
                        subtitles_data = version_response.get("subtitles", {})
                        all_subs = {**captions, **subtitles_data}

                        for lang_code, sub_data in all_subs.items():
                            if lang_code == "none":
                                continue

                            if isinstance(sub_data, dict) and "url" in sub_data:
                                try:
                                    lang = Language.get(lang_code)
                                except (ValueError, LookupError):
                                    lang = Language.get("fr")

                                subtitle_format = sub_data.get("format", "vtt").lower()
                                if subtitle_format == "ass" or subtitle_format == "ssa":
                                    codec = Subtitle.Codec.SubStationAlphav4
                                else:
                                    codec = Subtitle.Codec.WebVTT

                                tracks.add(
                                    Subtitle(
                                        id_=f"subtitle-{audio_locale}-{lang_code}",
                                        url=sub_data["url"],
                                        codec=codec,
                                        language=lang,
                                        forced=False,
                                        sdh=False,
                                    ),
                                    warn_only=True,
                                )

                    if version_token:
                        self.close_stream(request_episode_id, version_token)

                except Exception as e:
                    self.log.warning(f"Error processing endpoint {endpoint_key} for version {idx}: {e}")
                    continue

        if versions and versions[0].get("guid"):
            initial_token = initial_response.get("token")
            if initial_token:
                self.close_stream(episode_id, initial_token)

        if tracks is None:
            raise ValueError(f"Failed to fetch any tracks for episode: {episode_id}")

        for track in tracks.audio + tracks.subtitles:
            if track.language:
                try:
                    lang_obj = Language.get(str(track.language))
                    base_lang = Language.get(lang_obj.language)
                    lang_display = base_lang.language_name()
                    track.name = lang_display
                except (ValueError, LookupError):
                    pass

        return tracks

    def get_widevine_license(self, challenge: bytes, title: Episode, track) -> bytes:
        """
        Get Widevine license for decryption.

        Creates a fresh playback session for each track, gets the license, then immediately
        closes the stream. This prevents hitting the 3 concurrent stream limit.
        CDN authorization is embedded in the manifest URLs, not tied to active sessions.
        """
        self.ensure_authenticated()

        track_episode_id = track.data.get("episode_id", title.id)

        with self.LICENSE_LOCK:
            playback_token = None
            try:
                audio_locale = track.data.get("audio_locale")
                playback_data = self.get_playback_data(track_episode_id, track_stream=True, audio_locale=audio_locale)
                playback_token = playback_data.get("token")

                if not playback_token:
                    raise ValueError(f"No playback token in response for {track_episode_id}")

                track.data["playback_token"] = playback_token

                license_response = self.session.post(
                    url=self.config["endpoints"]["license_widevine"],
                    params={"specConform": "true"},
                    data=challenge,
                    headers={
                        **self.auth_header,
                        "content-type": "application/octet-stream",
                        "accept": "application/octet-stream",
                        "x-cr-content-id": track_episode_id,
                        "x-cr-video-token": playback_token,
                    },
                )
                if license_response.status_code != 200:
                    self.close_stream(track_episode_id, playback_token)
                    try:
                        error_detail = license_response.text[:200]
                    except Exception:
                        error_detail = "Unknown error"
                    raise ValueError(f"License request failed: {license_response.status_code} - {error_detail}")

                self.close_stream(track_episode_id, playback_token)
                return license_response.content

            except Exception:
                if playback_token:
                    try:
                        self.close_stream(track_episode_id, playback_token)
                    except Exception:
                        pass
                raise

    def cleanup_active_streams(self) -> None:
        """
        Close all remaining active streams.
        Called to ensure no streams are left open.
        """
        if self.ACTIVE_STREAMS:
            try:
                self.authenticate()
            except Exception as e:
                self.log.warning(f"Failed to re-authenticate during cleanup: {e}")

            for episode_id, token in list(self.ACTIVE_STREAMS):
                try:
                    self.close_stream(episode_id, token)
                except Exception as e:
                    self.log.warning(f"Failed to close stream {episode_id}: {e}")
                    if (episode_id, token) in self.ACTIVE_STREAMS:
                        self.ACTIVE_STREAMS.remove((episode_id, token))

    def __del__(self) -> None:
        """Cleanup any remaining streams when service is destroyed."""
        try:
            self.cleanup_active_streams()
        except Exception:
            pass

    def get_chapters(self, title: Episode) -> Chapters:
        """Get chapters/skip events for an episode."""
        chapters = Chapters()

        chapter_response = self.session.get(
            url=self.config["endpoints"]["skip_events"].format(episode_id=title.id),
        )
        special_chapters = []

        if chapter_response.status_code == 200:
            try:
                chapter_data = chapter_response.json()
            except Exception as e:
                self.log.warning(f"Failed to parse chapter data: {e}")
                return chapters

            for chapter_type in ["intro", "recap", "credits", "preview"]:
                if chapter_info := chapter_data.get(chapter_type):
                    try:
                        start_time = int(chapter_info["start"] * 1000)
                        end_time = int(chapter_info.get("end", chapter_info["start"]) * 1000)
                        special_chapters.append(
                            {
                                "start": start_time,
                                "end": end_time,
                                "name": chapter_info["type"].capitalize(),
                                "type": chapter_type,
                            }
                        )
                    except Exception:
                        pass

        special_chapters.sort(key=lambda x: x["start"])

        all_chapters = []
        chapter_counter = 1

        all_chapters.append({"timestamp": 0, "name": f"Chapter {chapter_counter}"})
        chapter_counter += 1

        for idx, special in enumerate(special_chapters):
            all_chapters.append({"timestamp": special["start"], "name": special["name"]})

            should_add_chapter_after = False

            if special["end"] > special["start"]:
                if idx + 1 < len(special_chapters):
                    next_special = special_chapters[idx + 1]
                    if next_special["start"] - special["end"] > 2000:
                        should_add_chapter_after = True
                else:
                    should_add_chapter_after = True

            if should_add_chapter_after:
                all_chapters.append({"timestamp": special["end"], "name": f"Chapter {chapter_counter}"})
                chapter_counter += 1

        for chapter in all_chapters:
            try:
                chapters.add(
                    Chapter(
                        timestamp=chapter["timestamp"],
                        name=chapter["name"],
                    )
                )
            except Exception:
                pass

        return chapters

    def search(self) -> Generator[SearchResult, None, None]:
        """Search for content on Crunchyroll."""
        try:
            response = self.session.get(
                url=self.config["endpoints"]["search"],
                params={
                    "q": self.title,
                    "type": "series",
                    "start": 0,
                    "n": 20,
                    "locale": self.config["params"]["locale"],
                },
            )

            if response.status_code != 200:
                raise ValueError(f"Search request failed with status {response.status_code}")

            search_data = response.json()
            for result_group in search_data.get("data", []):
                for series in result_group.get("items", []):
                    series_id = series.get("id")

                    if not series_id:
                        continue

                    title = series.get("title", "Unknown")
                    description = series.get("description", "")
                    year = series.get("series_launch_year")
                    if len(description) > 300:
                        description = description[:300] + "..."

                    url = f"https://www.crunchyroll.com/series/{series_id}"
                    label = f"SERIES ({year})" if year else "SERIES"

                    yield SearchResult(
                        id_=series_id,
                        title=title,
                        label=label,
                        description=description,
                        url=url,
                    )

        except Exception as e:
            raise ValueError(f"Search failed: {e}")

    def close_stream(self, episode_id: str, token: str) -> None:
        """Close an active playback stream to free up concurrent stream slots."""
        delete_url = self.config["endpoints"]["playback_delete"].format(episode_id=episode_id, token=token)
        closed = False

        for attempt in range(3):
            try:
                response = self.session.delete(url=delete_url, headers=self.auth_header)
                if response.status_code in (200, 204):
                    closed = True
                    break
                elif response.status_code == 403:
                    # Auth issue — re-authenticate and retry
                    if attempt < 2:
                        self.ensure_authenticated()
                        continue
                    self.log.warning(f"Could not close stream for {episode_id}: 403 after re-auth")
                    break
                else:
                    self.log.warning(
                        f"Failed to close stream for {episode_id} "
                        f"(status {response.status_code}, attempt {attempt + 1})"
                    )
                    if attempt < 2:
                        time.sleep(1)
            except Exception as e:
                self.log.warning(f"Error closing stream for {episode_id} (attempt {attempt + 1}): {e}")
                if attempt < 2:
                    time.sleep(1)

        # Always remove from local tracking to avoid stale entries
        if (episode_id, token) in self.ACTIVE_STREAMS:
            self.ACTIVE_STREAMS.remove((episode_id, token))

        if not closed:
            self.log.warning(f"Stream {episode_id}/{token[:12]}... may not have been released server-side")

    def get_active_sessions(self) -> list:
        """Get all active streaming sessions for the account."""
        try:
            response = self.session.get(
                url=self.config["endpoints"]["playback_sessions"],
                headers=self.auth_header,
            )
            if response.status_code == 200:
                data = response.json()
                items = data.get("items", [])
                return items
            else:
                self.log.warning(f"Failed to get active sessions (status {response.status_code})")
                return []
        except Exception as e:
            self.log.warning(f"Error getting active sessions: {e}")
            return []

    def clear_all_sessions(self) -> int:
        """
        Clear all active streaming sessions created during this or previous runs.

        Tries multiple approaches to ensure all streams are closed:
        1. Clear tracked streams with known tokens
        2. Query active sessions API and close all found streams
        3. Try alternate token formats if needed
        """
        cleared = 0

        if self.ACTIVE_STREAMS:
            streams_to_close = self.ACTIVE_STREAMS[:]
            for episode_id, playback_token in streams_to_close:
                try:
                    self.close_stream(episode_id, playback_token)
                    cleared += 1
                except Exception:
                    if (episode_id, playback_token) in self.ACTIVE_STREAMS:
                        self.ACTIVE_STREAMS.remove((episode_id, playback_token))

        sessions = self.get_active_sessions()
        if sessions:
            for session_data in sessions:
                content_id = session_data.get("contentId")
                session_token = session_data.get("token")

                if content_id and session_token:
                    tokens_to_try = (
                        ["11-" + session_token[3:], session_token]
                        if session_token.startswith("08-")
                        else [session_token]
                    )

                    session_closed = False
                    for token in tokens_to_try:
                        try:
                            response = self.session.delete(
                                url=self.config["endpoints"]["playback_delete"].format(
                                    episode_id=content_id, token=token
                                ),
                                headers=self.auth_header,
                            )
                            if response.status_code in (200, 204):
                                cleared += 1
                                session_closed = True
                                break
                            elif response.status_code == 403:
                                session_closed = True
                                break
                        except Exception:
                            pass

                    if not session_closed:
                        self.log.warning(f"Unable to close session {content_id} with any token format")

        return cleared

    def get_playback_data(
        self,
        episode_id: str,
        track_stream: bool = True,
        endpoint_key: str = "playback",
        audio_locale: Optional[str] = None,
    ) -> dict:
        """
        Get playback data for an episode with automatic retry on stream limits.

        Args:
            episode_id: The episode ID to get playback data for
            track_stream: Whether to track this stream in active_streams (False for temporary streams)
            endpoint_key: Which endpoint to use ('playback' or 'playback_download')
            audio_locale: Preferred audio locale (e.g. 'en-US', 'ja-JP')

        Returns:
            dict: The playback response data

        Raises:
            ValueError: If playback request fails after retry
        """
        self.ensure_authenticated()

        params: dict[str, str] = {"queue": "false"}
        if audio_locale:
            params["audio"] = audio_locale

        max_retries = 3
        for attempt in range(max_retries + 1):
            try:
                http_response = self.session.get(
                    url=self.config["endpoints"][endpoint_key].format(episode_id=episode_id),
                    params=params,
                )
            except Exception as e:
                # Session layer exhausted its own retries (e.g. repeated 429s)
                if attempt < max_retries:
                    self.log.warning(f"Playback request failed for {episode_id}: {e}")
                    self._recover_from_rate_limit(attempt)
                    continue
                raise ValueError(f"Playback request failed for {episode_id} after retries: {e}")

            if http_response.status_code == 429:
                if attempt < max_retries:
                    self.log.warning(f"Rate limited (429) on playback for {episode_id}")
                    self._recover_from_rate_limit(attempt)
                    continue
                raise ValueError(f"Rate limited on playback for {episode_id} after {max_retries} retries")

            try:
                response = http_response.json()
            except Exception as e:
                raise ValueError(f"Playback: failed to parse JSON (episode_id={episode_id}): {e}")

            if "error" in response:
                error_code = response.get("code", "")
                error_msg = response.get("message", response.get("error", "Unknown error"))

                if error_code == "TOO_MANY_ACTIVE_STREAMS" and attempt < max_retries:
                    self.log.warning(f"Hit stream limit: {error_msg}")
                    self._recover_from_rate_limit(attempt)
                    continue

                raise ValueError(f"Could not get playback info for episode: {episode_id} - {error_msg}")

            playback_token = response.get("token")
            if playback_token and track_stream:
                self.ACTIVE_STREAMS.append((episode_id, playback_token))

            return response

        raise ValueError(f"Failed to get playback data for episode: {episode_id}")

    def _recover_from_rate_limit(self, attempt: int) -> None:
        """Clear all sessions and wait with exponential backoff before retrying."""
        cleared = self.clear_all_sessions()
        wait_time = min(5 * (2**attempt), 30)
        if cleared == 0:
            wait_time = max(wait_time, 15)
        self.log.warning(f"Cleared {cleared} sessions, waiting {wait_time}s before retry...")
        time.sleep(wait_time)

    def parse_series_id(self, title_input: str) -> str:
        """Parse series ID from URL or direct ID input."""
        match = re.match(self.TITLE_RE, title_input, re.IGNORECASE)
        if not match:
            raise ValueError(f"Could not parse series ID from: {title_input}")
        series_id = match.group("id")
        return series_id
