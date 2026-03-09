import logging
from datetime import timedelta, datetime, timezone
from uuid import UUID

import httpx
from fastapi import HTTPException
from starlette.status import HTTP_400_BAD_REQUEST, HTTP_500_INTERNAL_SERVER_ERROR

from app.config import settings
from app.database import DbSession
from app.schemas import (
    AuthenticationMethod,
    OAuthTokenResponse,
    ProviderCredentials,
    ProviderEndpoints,
)
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)


class WithingsOAuth(BaseOAuthTemplate):
    """Withings OAuth 2.0 implementation.

    Withings has a non-standard OAuth2 flow:
    - Token endpoint requires an 'action' parameter
    - Responses are wrapped in {"status": 0, "body": {token_fields...}}
    - The userid is returned directly in the token response body
    """

    @property
    def endpoints(self) -> ProviderEndpoints:
        """OAuth endpoints for authorization and token exchange."""
        return ProviderEndpoints(
            authorize_url="https://account.withings.com/oauth2_user/authorize2",
            token_url="https://wbsapi.withings.net/v2/oauth2",
        )

    @property
    def credentials(self) -> ProviderCredentials:
        """OAuth credentials from environment variables."""
        return ProviderCredentials(
            client_id=settings.withings_client_id or "",
            client_secret=(settings.withings_client_secret.get_secret_value() if settings.withings_client_secret else ""),
            redirect_uri=settings.withings_redirect_uri,
            default_scope=settings.withings_default_scope,
        )

    # OAuth configuration
    use_pkce: bool = False
    auth_method: AuthenticationMethod = AuthenticationMethod.BODY

    # Store the userid from token exchange for use in _get_provider_user_info
    _last_token_userid: str | None = None

    def _parse_withings_token_response(self, response_json: dict) -> OAuthTokenResponse:
        """Parse Withings wrapped token response.

        Withings returns: {"status": 0, "body": {"access_token": ..., "userid": ..., ...}}
        We need to extract from "body" and map to OAuthTokenResponse.
        """
        status = response_json.get("status")
        if status != 0:
            error_msg = response_json.get("error", f"Withings API error status: {status}")
            raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(error_msg))

        body = response_json.get("body", {})

        # Store userid for later use in _get_provider_user_info
        userid = body.get("userid")
        if userid is not None:
            self._last_token_userid = str(userid)

        return OAuthTokenResponse(
            access_token=body["access_token"],
            token_type=body.get("token_type", "Bearer"),
            refresh_token=body.get("refresh_token"),
            expires_in=body.get("expires_in", 10800),
            scope=body.get("scope"),
        )

    def _prepare_token_request(self, code: str, code_verifier: str | None) -> tuple[dict, dict]:
        """Prepare Withings token exchange request with action parameter."""
        creds = self.credentials
        token_data = {
            "action": "requesttoken",
            "grant_type": "authorization_code",
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "code": code,
            "redirect_uri": creds.redirect_uri,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        return token_data, headers

    def _exchange_token(self, code: str, code_verifier: str | None) -> OAuthTokenResponse:
        """Exchange authorization code for tokens (Withings-specific response parsing)."""
        data, headers = self._prepare_token_request(code, code_verifier)

        try:
            response = httpx.post(
                self.endpoints.token_url,
                data=data,
                headers=headers,
                timeout=30.0,
            )
            response.raise_for_status()
            return self._parse_withings_token_response(response.json())
        except HTTPException:
            raise
        except httpx.HTTPStatusError as e:
            log_structured(
                logger,
                "error",
                f"Failed to exchange authorization code: {e.response.text}",
                provider=self.provider_name,
                task="exchange_token",
                status_code=e.response.status_code,
            )
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=f"Failed to exchange authorization code: {e.response.text}",
            )
        except Exception as e:
            log_structured(
                logger,
                "error",
                f"Token exchange failed: {e}",
                provider=self.provider_name,
                task="exchange_token",
            )
            raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Token exchange failed: {str(e)}")

    def _prepare_refresh_request(self, refresh_token: str) -> tuple[dict, dict]:
        """Prepare Withings token refresh request with action parameter."""
        creds = self.credentials
        token_data = {
            "action": "requesttoken",
            "grant_type": "refresh_token",
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "refresh_token": refresh_token,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        return token_data, headers

    def refresh_access_token(self, db: DbSession, user_id: UUID, refresh_token: str) -> OAuthTokenResponse:
        """Refresh the access token (Withings-specific response parsing)."""
        data, headers = self._prepare_refresh_request(refresh_token)

        try:
            response = httpx.post(
                self.endpoints.token_url,
                data=data,
                headers=headers,
                timeout=30.0,
            )
            response.raise_for_status()
            token_response = self._parse_withings_token_response(response.json())

            connection = self.connection_repo.get_by_user_and_provider(db, user_id, self.provider_name)
            if connection:
                self.connection_repo.update_tokens(
                    db,
                    connection,
                    token_response.access_token,
                    token_response.refresh_token or refresh_token,
                    token_response.expires_in,
                )

            log_structured(
                logger,
                "info",
                "OAuth token refreshed successfully",
                provider=self.provider_name,
                task="refresh_access_token",
                user_id=str(user_id),
            )

            return token_response

        except HTTPException:
            raise
        except httpx.HTTPStatusError as e:
            log_structured(
                logger,
                "error",
                f"Failed to refresh OAuth token: {e.response.text}",
                provider=self.provider_name,
                task="refresh_access_token",
                user_id=str(user_id),
                status_code=e.response.status_code,
            )
            raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=f"Failed to refresh token: {e.response.text}")
        except Exception as e:
            log_structured(
                logger,
                "error",
                f"OAuth token refresh failed: {e}",
                provider=self.provider_name,
                task="refresh_access_token",
                user_id=str(user_id),
            )
            raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Token refresh failed: {str(e)}")

    def _get_provider_user_info(self, token_response: OAuthTokenResponse, user_id: str) -> dict[str, str | None]:
        """Get Withings user ID.

        The userid is already captured during token exchange from the response body,
        so we use the cached value rather than making another API call.
        """
        provider_user_id = self._last_token_userid

        if provider_user_id:
            log_structured(
                logger,
                "info",
                "Got Withings user ID from token response",
                provider="withings",
                task="get_provider_user_info",
                user_id=user_id,
            )
        else:
            log_structured(
                logger,
                "warning",
                "No Withings user ID found in token response",
                provider="withings",
                task="get_provider_user_info",
                user_id=user_id,
            )

        return {"user_id": provider_user_id, "username": None}
