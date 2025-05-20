import os
import json
import enum
import time
import asyncio
import logging
from dataclasses import dataclass
from typing import Any
import backoff
import httpx
from mcp.server.fastmcp import FastMCP


### BlackbirApiClient
BASE_URL = "https://api.blackbird.ai"
BLACKBIRD_CONTEXT_MAX_RETRIES = 60
BLACKBIRD_CONTEXT_MAX_TIME = 600
BLACKBIRD_CONTEXT_CHECK_INTERVAL = 10

logger = logging.getLogger(__name__)


class RetryableError(RuntimeError):
    pass


@dataclass
class Token:
    token: str
    expiration_time: float


class ResourceType(enum.Enum):
    CONTEXT = "compass/contextChecks"
    VISION = "compass/visionAnalyses"


class BlackbirdApiClient:
    _auth_url: str
    _auth_payload: dict
    token: Token

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ):
        if username and password:
            self._auth_url = f"{BASE_URL}/compass/token"
            self._auth_payload = {
                "username": username,
                "password": password,
                "grant_type": "password",
            }
        elif client_id and client_secret:
            self._auth_url = "https://blackbird-ai.auth.us-west-2.amazoncognito.com/oauth2/token"
            self._auth_payload = {
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
            }
        else:
            raise ValueError("Either username and password or client_id and client_secret must be provided")

        self._session = httpx.AsyncClient()
        self.token = self.__new_token()

    def __new_token(self) -> Token:
        response = httpx.post(self._auth_url, data=self._auth_payload)
        match data := response.json():
            case {"error": _}:
                raise RuntimeError(data)
            case {"access_token": token, "expires_in": expires_in}:
                return Token(token, time.time() + expires_in)
            case _:
                raise RuntimeError(f"Failed to get token with {response=}")

    @property
    def bearer_token(self) -> str:
        if time.time() - 0.5 > self.token.expiration_time:
            # buffer of 0.5 seconds for safety
            self.token = self.__new_token()
        return f"Bearer {self.token.token}"

    @property
    def headers(self):
        return {
            "Authorization": self.bearer_token,
            "Content-Type": "application/json",
        }

    @backoff.on_exception(backoff.expo, RetryableError, max_tries=3)
    async def _submit_resource_type(self, resource: str, resource_type: ResourceType, opts: dict = {}) -> str:
        data: dict[str, Any] = {"input": resource}
        if opts:
            data["options"] = opts
        response = await self._session.post(
            f"{BASE_URL}/{resource_type.value}",
            headers=self.headers,
            json=data,
        )
        match response:
            case httpx.Response(status_code=200 | 202):
                data = response.json()
                return data["id"]
            case httpx.Response(status_code=500):
                raise RetryableError(f"Failed to submit context with {response=}")
            case _:
                raise RuntimeError(f"Failed to submit context with {response=}")

    @backoff.on_exception(backoff.expo, RetryableError, max_tries=3)
    async def _check_resource_type(self, resource_id: str, resource_type: ResourceType) -> dict:
        response = await self._session.get(
            f"{BASE_URL}/{resource_type.value}/{resource_id}",
            headers=self.headers,
        )
        match response:
            case httpx.Response(status_code=200 | 202):
                data = response.json()
                return data
            case httpx.Response(status_code=500):
                raise RetryableError(f"Failed to check{resource_type} {resource_id}")
            case httpx.Response(status_code=status_code) if 500 > status_code >= 400:
                raise RuntimeError(f"{resource_type} {resource_id} was not found with {response=}")
            case _:
                raise RuntimeError(f"Failed to submit context with {response=}")

    async def submit_and_wait_resource(
        self,
        resource: str,
        resource_type: ResourceType,
        opts: dict = {},
        max_retries: int = BLACKBIRD_CONTEXT_MAX_RETRIES,
        max_time: int = BLACKBIRD_CONTEXT_MAX_TIME,
        check_interval: int = BLACKBIRD_CONTEXT_CHECK_INTERVAL,
    ) -> dict:
        resource_id = await self._submit_resource_type(resource, resource_type, opts)
        logger.info(f"Submitted {resource_id=}")
        retries = 0
        error = ""
        cutoff = time.time() + max_time
        while retries <= max_retries and time.time() < cutoff:
            check = await self._check_resource_type(resource_id, resource_type)
            match check:
                case {"status": "success", "context": data, "input": _input}:
                    return {**data, "input": _input}
                case {
                    "status": "success",
                    "options": options,
                    "input": _input,
                    "analysis": analysis,
                }:
                    return {**analysis, "options": options, "input": _input}
                case {"status": "processing"}:
                    await asyncio.sleep(check_interval)
                    retries += 1
                case {"status": "failed", "error": err}:
                    error = err
                    logger.warning(f"Failed {check=} with {err=}")
                    await asyncio.sleep(check_interval)
                    retries += 1
                case _:
                    logger.warning(f"Retrying unknown {check=}")
                    await asyncio.sleep(check_interval)
                    retries += 1
        raise RuntimeError(f"Retried {retries} times for {max_time}s with {error=}")


### Server

mcp = FastMCP("blackbird-mcp-server")
BLACKBIRD_CLIENT_KEY = os.environ.get("BLACKBIRD_CLIENT_KEY", "")
BLACKBIRD_SECRET_KEY = os.environ.get("BLACKBIRD_SECRET_KEY", "")
BLACKBIRD_USERNAME = os.environ.get("BLACKBIRD_USERNAME", "")
BLACKBIRD_PASSWORD = os.environ.get("BLACKBIRD_PASSWORD", "")

blackbird_api = BlackbirdApiClient(
    client_id=BLACKBIRD_CLIENT_KEY,
    client_secret=BLACKBIRD_SECRET_KEY,
    username=BLACKBIRD_USERNAME,
    password=BLACKBIRD_PASSWORD,
)


@mcp.tool()
async def check_context(context: str) -> str:
    """
    Tool to check if a given context has truthful claims or not.
    Additionaly, it measures how risky the claims are in the given context.
    Useful for fact checking.
    """
    result = await blackbird_api.submit_and_wait_resource(context, ResourceType.CONTEXT)
    return json.dumps(result)


@mcp.tool()
async def check_vision(url: str) -> str:
    """Tool to check if a given image is fake or ai-generated with an explanation."""
    result = await blackbird_api.submit_and_wait_resource(url, ResourceType.VISION, opts={"explain": True})
    return json.dumps(result)


if __name__ == "__main__":
    # Initialize and run the server
    mcp.run(transport="stdio")
